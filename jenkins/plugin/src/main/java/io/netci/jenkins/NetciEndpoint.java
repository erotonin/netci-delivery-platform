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
        try {
            body = JSONObject.fromObject(readBody(req));
        } catch (JSONException | IOException e) {
            return error(400, "the body must be a JSON object of at most 64 KiB");
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
