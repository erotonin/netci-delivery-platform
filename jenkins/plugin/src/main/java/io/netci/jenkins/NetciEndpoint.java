package io.netci.jenkins;

import hudson.Extension;
import hudson.model.Action;
import hudson.model.CauseAction;
import hudson.model.Item;
import hudson.model.Job;
import hudson.model.ParameterDefinition;
import hudson.model.ParameterValue;
import hudson.model.ParametersAction;
import hudson.model.ParametersDefinitionProperty;
import hudson.model.Queue;
import hudson.model.Result;
import hudson.model.RootAction;
import hudson.model.Run;
import hudson.model.SimpleParameterDefinition;
import hudson.model.queue.ScheduleResult;
import hudson.security.ACL;
import hudson.security.ACLContext;
import io.netci.jenkins.homesync.RunsAcrossReplicas;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.regex.Pattern;
import jenkins.model.Jenkins;
import jenkins.model.ParameterizedJobMixIn;
import net.sf.json.JSONException;
import net.sf.json.JSONObject;
import org.kohsuke.stapler.HttpResponse;
import org.kohsuke.stapler.StaplerRequest2;
import org.kohsuke.stapler.StaplerResponse2;
import org.kohsuke.stapler.interceptor.RequirePOST;

/**
 * {@code /netci/}: the controller side of netCI's dispatch (ADR-063).
 *
 * <ul>
 *   <li>{@code POST /netci/dispatch} {@code {"job", "runId", "parameters", "notBefore",
 *       "requestedBy"}}: returns where the run is, or schedules it if it is nowhere -- one
 *       atomic step under the queue lock, so repeating the call never starts the run twice.
 *   <li>{@code GET /netci/run?job=&runId=&notBefore=}: the same lookup, scheduling nothing.
 * </ul>
 *
 * Every answer carries {@code session}, new on every start of this JVM: a dispatcher that sees
 * it change knows the queue it dispatched into may be gone, and dispatches again.
 */
@Extension
public final class NetciEndpoint implements RootAction {
    /** Identifies this start of the controller. */
    static final String SESSION = UUID.randomUUID().toString();

    private static final int MAX_BODY = 64 * 1024;
    private static final Pattern RUN_ID =
            Pattern.compile("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$");

    @Override
    public String getIconFileName() {
        return null;
    }

    @Override
    public String getDisplayName() {
        return null;
    }

    @Override
    public String getUrlName() {
        return "netci";
    }

    @RequirePOST
    public HttpResponse doDispatch(StaplerRequest2 req) throws Exception {
        JSONObject body;
        String raw;
        try {
            raw = readBody(req);
            body = JSONObject.fromObject(raw);
        } catch (JSONException | IOException e) {
            return error(400, "the body must be a JSON object of at most 64 KiB");
        }
        if (RunsAcrossReplicas.configured() && !RunsAcrossReplicas.active()) {
            return error(503, "this replica is catching up with the replicas' store; retry");
        }
        HttpResponse routed = route(req, "POST", "/netci/dispatch", raw, body.optString("job", ""));
        if (routed != null) {
            return routed;
        }
        String runId = body.optString("runId", "");
        if (!RUN_ID.matcher(runId).matches()) {
            return error(400, "runId must be a lower-case UUID");
        }
        Job<?, ?> job = Jenkins.get().getItemByFullName(body.optString("job", ""), Job.class);
        if (job == null) {
            return error(404, "no such job, or no permission to see it");
        }
        if (!(job instanceof ParameterizedJobMixIn.ParameterizedJob<?, ?> pj)) {
            return error(400, "this kind of job cannot be scheduled");
        }
        job.checkPermission(Item.BUILD);
        Object params = body.opt("parameters");
        List<Action> actions = new ArrayList<>();
        try {
            ParametersAction p = parameters(job, params == null ? new JSONObject() : params);
            if (p != null) {
                actions.add(p);
            }
        } catch (IllegalArgumentException e) {
            return error(400, e.getMessage());
        }
        actions.add(new NetciRunAction(runId));
        actions.add(new CauseAction(new NetciCause(runId, body.optString("requestedBy", ""))));
        if (!pj.isBuildable()) {
            return error(409, "the job is disabled");
        }
        long notBefore = body.optLong("notBefore", 0);

        Callable<JSONObject> here = () -> {
            try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
                RunLookup.Found found = RunLookup.find(job, runId, notBefore);
                return found == null ? null : describe(found, false);
            }
        };
        JSONObject known = Queue.withLock(here);
        if (known != null) {
            return json(200, known.element("runId", runId).element("job", job.getFullName()));
        }
        // Not here: has another replica got it? Asked outside the queue's lock -- the store is a
        // network call -- and not trusted alone: the reservation refuses a second number.
        if (RunsAcrossReplicas.active()) {
            try {
                RunsAcrossReplicas.Elsewhere e = RunsAcrossReplicas.find(runId);
                if (e != null) {
                    return json(200, elsewhere(e).element("runId", runId).element("job", job.getFullName()));
                }
            } catch (IOException e) {
                return error(503, "the replicas' store cannot be reached to tell whether the run exists; retry");
            }
        }
        Callable<JSONObject> dispatch = () -> {
            // The lookup must see every item and build, whatever the caller may read; the caller
            // was checked for BUILD on this job above and learns only about its own run.
            try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
                RunLookup.Found found = RunLookup.find(job, runId, notBefore);
                if (found != null) {
                    return describe(found, false);
                }
                ScheduleResult r = Jenkins.get().getQueue().schedule2(pj, pj.getQuietPeriod(), actions);
                Queue.Item item = r.getItem();
                if (r.isRefused() || item == null) {
                    return null;
                }
                return describe(new RunLookup.Found(RunLookup.State.QUEUED, item.getId(), null), r.isCreated());
            }
        };
        JSONObject result = Queue.withLock(dispatch);
        if (result == null && RunsAcrossReplicas.configured()) {
            // With replicas the refusal is the owner's rule or the store, both passing: never a
            // refusal of the run for good, which netci-queue takes a 409 to be.
            return error(503, "this replica cannot schedule the run now (another replica owns the job, or the store is unreachable); retry");
        }
        if (result == null) {
            return error(409, "Jenkins refused to schedule the run");
        }
        return json(200, result.element("runId", runId).element("job", job.getFullName()));
    }

    public HttpResponse doRun(StaplerRequest2 req) throws Exception {
        String runId = req.getParameter("runId");
        if (runId == null || !RUN_ID.matcher(runId).matches()) {
            return error(400, "runId must be a lower-case UUID");
        }
        String name = req.getParameter("job");
        if (RunsAcrossReplicas.configured() && !RunsAcrossReplicas.active()) {
            return error(503, "this replica is catching up with the replicas' store; retry");
        }
        HttpResponse routed = route(req, "GET", "/netci/run?" + req.getQueryString(), null, name == null ? "" : name);
        if (routed != null) {
            return routed;
        }
        Job<?, ?> job = Jenkins.get().getItemByFullName(name == null ? "" : name, Job.class);
        if (job == null) {
            return error(404, "no such job, or no permission to see it");
        }
        long notBefore;
        try {
            notBefore = req.getParameter("notBefore") == null ? 0 : Long.parseLong(req.getParameter("notBefore"));
        } catch (NumberFormatException e) {
            return error(400, "notBefore must be epoch milliseconds");
        }
        Callable<JSONObject> lookup = () -> {
            try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
                RunLookup.Found found = RunLookup.find(job, runId, notBefore);
                return found == null ? null : describe(found, false);
            }
        };
        JSONObject result = Queue.withLock(lookup);
        if (result == null && RunsAcrossReplicas.active()) {
            try {
                RunsAcrossReplicas.Elsewhere e = RunsAcrossReplicas.find(runId);
                if (e != null) {
                    result = elsewhere(e);
                }
            } catch (IOException e) {
                // "absent" would end a running build's run (finished, result unknown): say nothing
                return error(503, "the replicas' store cannot be reached to tell where the run is; retry");
            }
        }
        if (result == null) {
            return json(404, new JSONObject().element("runId", runId).element("state", "absent").element("session", SESSION));
        }
        return json(200, result.element("runId", runId).element("job", job.getFullName()));
    }

    /**
     * Values for the job's declared parameters: those given, defaults for the rest. A parameter
     * the job does not declare is refused rather than dropped, and so is a non-string value.
     */
    static ParametersAction parameters(Job<?, ?> job, Object given) {
        if (!(given instanceof JSONObject values)) {
            throw new IllegalArgumentException("parameters must be an object of strings");
        }
        ParametersDefinitionProperty pdp = job.getProperty(ParametersDefinitionProperty.class);
        for (Iterator<?> it = values.keys(); it.hasNext(); ) {
            String name = (String) it.next();
            if (pdp == null || pdp.getParameterDefinition(name) == null) {
                throw new IllegalArgumentException("the job does not declare parameter " + name);
            }
            if (!(values.get(name) instanceof String)) {
                throw new IllegalArgumentException("parameter " + name + " must be a string");
            }
        }
        if (pdp == null) {
            return null;
        }
        List<ParameterValue> out = new ArrayList<>();
        for (ParameterDefinition d : pdp.getParameterDefinitions()) {
            ParameterValue v;
            if (values.containsKey(d.getName())) {
                if (!(d instanceof SimpleParameterDefinition s)) {
                    throw new IllegalArgumentException("parameter " + d.getName() + " cannot be given as a string");
                }
                v = s.createValue(values.getString(d.getName()));
            } else {
                v = d.getDefaultParameterValue();
            }
            if (v != null) {
                out.add(v);
            }
        }
        return new ParametersAction(out);
    }

    /**
     * netci-queue's cancel of a run (the IDP's abort, a superseded build), by run id: wherever the
     * run is. In this controller's queue it is taken out ("cancelled"); building here it is
     * interrupted and ends ABORTED ("stopping"); building on another replica (adopted there), the
     * call goes to that replica; nowhere, "absent". A run between the queue and its build
     * ("starting") is a 503: asked again, it is one or the other.
     */
    @RequirePOST
    public HttpResponse doCancel(StaplerRequest2 req) throws Exception {
        JSONObject body;
        String raw;
        try {
            raw = readBody(req);
            body = JSONObject.fromObject(raw);
        } catch (JSONException | IOException e) {
            return error(400, "the body must be a JSON object of at most 64 KiB");
        }
        String runId = body.optString("runId", "");
        if (!RUN_ID.matcher(runId).matches()) {
            return error(400, "runId must be a lower-case UUID");
        }
        if (RunsAcrossReplicas.configured() && !RunsAcrossReplicas.active()) {
            return error(503, "this replica is catching up with the replicas' store; retry");
        }
        HttpResponse routed = route(req, "POST", "/netci/cancel", raw, body.optString("job", ""));
        if (routed != null) {
            return routed;
        }
        Job<?, ?> job = Jenkins.get().getItemByFullName(body.optString("job", ""), Job.class);
        if (job == null) {
            return error(404, "no such job, or no permission to see it");
        }
        job.checkPermission(Item.CANCEL);
        long notBefore = body.optLong("notBefore", 0);
        Callable<JSONObject> here = () -> {
            try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
                RunLookup.Found f = RunLookup.find(job, runId, notBefore);
                if (f == null) {
                    return null;
                }
                switch (f.state()) {
                    case QUEUED: {
                        Queue q = Jenkins.get().getQueue();
                        Queue.Item item = q.getItem(f.queueId());
                        return new JSONObject().element("state", item != null && q.cancel(item) ? "cancelled" : "starting");
                    }
                    case STARTING:
                        return new JSONObject().element("state", "starting");
                    default: {
                        Run<?, ?> b = f.build();
                        JSONObject o = describe(f, false);
                        if (b.isBuilding()) {
                            hudson.model.Executor e = b.getExecutor();
                            if (e == null) {
                                return new JSONObject().element("state", "starting");
                            }
                            e.interrupt(Result.ABORTED);
                            return o.element("state", "stopping");
                        }
                        return o.element("state", "ended");
                    }
                }
            }
        };
        JSONObject result = Queue.withLock(here);
        if (result == null && RunsAcrossReplicas.active()) {
            try {
                RunsAcrossReplicas.Elsewhere e = RunsAcrossReplicas.find(runId);
                String auth = req.getHeader("Authorization");
                if (e != null && e.started() && auth != null && !e.replica().equals(io.netci.jenkins.homesync.RunsAcrossReplicas.me())) {
                    RunsAcrossReplicas.Answer a = RunsAcrossReplicas.forward(e.replica(), "POST", req.getContextPath() + "/netci/cancel", raw, auth);
                    return new HttpResponse() {
                        @Override
                        public void generateResponse(StaplerRequest2 r, StaplerResponse2 rsp, Object node) throws IOException {
                            rsp.setStatus(a.status());
                            rsp.setContentType("application/json;charset=UTF-8");
                            rsp.setHeader("X-Netci-Answered-By", e.replica());
                            rsp.getWriter().print(a.body());
                        }
                    };
                }
                if (e != null) {
                    return error(503, "the run is about to start on replica " + e.replica() + "; retry");
                }
            } catch (IOException e) {
                return error(503, "the run's replica or the replicas' store cannot be reached; retry");
            }
        }
        if (result == null) {
            return json(404, new JSONObject().element("runId", runId).element("state", "absent").element("session", SESSION));
        }
        if ("starting".equals(result.getString("state"))) {
            return error(503, "the run is between the queue and its build; retry");
        }
        return json(200, result.element("runId", runId).element("job", job.getFullName()).element("session", SESSION));
    }

    /**
     * A call for a job another replica owns, made there (ADR-081), or null to answer here: homesync
     * off, the call already routed, this replica the owner, or no credential to act with.
     */
    private static HttpResponse route(StaplerRequest2 req, String method, String pathAndQuery, String body, String job) {
        if (!RunsAcrossReplicas.active() || req.getHeader(RunsAcrossReplicas.ROUTED) != null) {
            return null;
        }
        String auth = req.getHeader("Authorization");
        try {
            String owner = RunsAcrossReplicas.ownerElsewhere(job);
            if (owner == null || auth == null) {
                return null;
            }
            RunsAcrossReplicas.Answer a = RunsAcrossReplicas.forward(owner, method, req.getContextPath() + pathAndQuery, body, auth);
            return new HttpResponse() {
                @Override
                public void generateResponse(StaplerRequest2 r, StaplerResponse2 rsp, Object node) throws IOException {
                    rsp.setStatus(a.status());
                    rsp.setContentType("application/json;charset=UTF-8");
                    rsp.setHeader("X-Netci-Answered-By", owner);
                    rsp.getWriter().print(a.body());
                }
            };
        } catch (IOException e) {
            return error(503, "the owner of the job cannot be reached, or the store cannot say which replica it is; retry");
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return error(503, "interrupted; retry");
        }
    }

    /** A run another replica has: started (running there, or finished and not yet listed here) or about to start. */
    private static JSONObject elsewhere(RunsAcrossReplicas.Elsewhere e) {
        JSONObject o = new JSONObject().element("state", e.started() ? "started" : "starting").element("created", false)
                .element("session", SESSION).element("replica", e.replica());
        if (e.started()) {
            String url;
            try (ACLContext ignored = ACL.as2(ACL.SYSTEM2)) {
                Job<?, ?> j = Jenkins.get().getItemByFullName(e.job(), Job.class);
                url = (j == null ? "job/" + e.job() + "/" : j.getUrl()) + e.number() + "/";
            }
            o.element("build", new JSONObject().element("number", e.number()).element("building", true).element("url", url));
        }
        return o;
    }

    private static JSONObject describe(RunLookup.Found f, boolean created) {
        JSONObject o = new JSONObject()
                .element("state", f.state().name().toLowerCase(java.util.Locale.ROOT))
                .element("created", created)
                .element("session", SESSION);
        if (f.queueId() >= 0) {
            o.element("queueId", f.queueId());
        }
        Run<?, ?> b = f.build();
        if (b != null) {
            JSONObject build = new JSONObject()
                    .element("number", b.getNumber())
                    .element("building", b.isBuilding())
                    .element("url", b.getUrl());
            Result result = b.getResult();
            if (result != null) {
                build.element("result", result.toString());
            }
            o.element("build", build);
        }
        return o;
    }

    private static String readBody(StaplerRequest2 req) throws IOException {
        try (InputStream in = req.getInputStream()) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) {
                if (out.size() + n > MAX_BODY) {
                    throw new IOException("body too large");
                }
                out.write(buf, 0, n);
            }
            return out.toString(StandardCharsets.UTF_8);
        }
    }

    private static HttpResponse error(int status, String message) {
        return json(status, new JSONObject().element("error", message).element("session", SESSION));
    }

    private static HttpResponse json(int status, JSONObject body) {
        return new HttpResponse() {
            @Override
            public void generateResponse(StaplerRequest2 req, StaplerResponse2 rsp, Object node) throws IOException {
                rsp.setStatus(status);
                rsp.setContentType("application/json;charset=UTF-8");
                rsp.getWriter().print(body.toString());
            }
        };
    }
}
