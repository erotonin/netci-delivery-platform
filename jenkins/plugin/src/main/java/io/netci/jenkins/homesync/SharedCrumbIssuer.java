package io.netci.jenkins.homesync;

import hudson.Extension;
import hudson.Util;
import hudson.init.InitMilestone;
import hudson.init.Initializer;
import hudson.security.csrf.CrumbIssuer;
import hudson.security.csrf.CrumbIssuerDescriptor;
import hudson.security.csrf.DefaultCrumbIssuer;
import jakarta.servlet.ServletRequest;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.logging.Logger;
import jenkins.model.Jenkins;
import jenkins.security.HexStringConfidentialKey;
import jenkins.util.SystemProperties;
import org.jenkinsci.Symbol;
import org.kohsuke.stapler.DataBoundConstructor;

/**
 * A CSRF crumb every replica accepts (ADR-086). Jenkins binds its crumb to the user and the HTTP
 * session, and a session is one JVM's: a form from jenkins-0 posted to jenkins-1 -- the balancer's
 * retry of a 503, or the person's next click after jenkins-0 died -- was refused with 403 ("No
 * valid crumb"), although the person was signed in on both (ADR-077).
 *
 * <p>For a request with a shared sign-in, the crumb is bound to the user and that sign-in's id
 * ({@link SharedSession#crumbBinding}), under the same salt Jenkins uses (its {@code crumbSalt}
 * secret, the same on every replica). It is as strong as Jenkins' own: the sid is in an HttpOnly
 * cookie a page from another site cannot read, signed so it cannot be chosen, new at each sign-in,
 * and gone at sign-out. A request without one (an API token needs no crumb; a session from before
 * the sid) gets Jenkins' own crumb, unchanged, and either kind is accepted where it is valid.
 *
 * <p>Installed at start-up in place of Jenkins' default when shared sign-ins are on; a site that
 * configures a crumb issuer of its own keeps it (JCasC: {@code crumbIssuer: netciSharedSession}).
 * Installing it does not save Jenkins' configuration, but the next save puts it in config.xml, and
 * from then on it is read back from there (see {@link #readResolve}).
 */
public final class SharedCrumbIssuer extends DefaultCrumbIssuer {
    private static final Logger LOGGER = Logger.getLogger(SharedCrumbIssuer.class.getName());

    @DataBoundConstructor
    public SharedCrumbIssuer() {}

    /**
     * Read back from config.xml, it is made anew. XStream builds an object without its constructor,
     * and DefaultCrumbIssuer's readResolve, which sets up its digest, is private -- so it is not
     * run for a subclass, and every Jenkins-form crumb was null (measured: after a restart both
     * replicas answered {@code "crumb": null}, and every form was refused).
     */
    protected Object readResolve() {
        return new SharedCrumbIssuer();
    }

    @Override
    protected String issueCrumb(ServletRequest request, String salt) {
        String signIn = SharedSession.crumbBinding(request);
        return signIn == null ? super.issueCrumb(request, salt) : bound(signIn, salt);
    }

    @Override
    public boolean validateCrumb(ServletRequest request, String salt, String crumb) {
        if (crumb == null || salt == null) {
            return false;
        }
        String signIn = SharedSession.crumbBinding(request);
        if (signIn != null && same(bound(signIn, salt), crumb)) {
            return true;
        }
        // Jenkins' own form, computed by its own code: DefaultCrumbIssuer.validateCrumb would call
        // issueCrumb above and so compare against the bound form twice.
        return same(super.issueCrumb(request, salt), crumb);
    }

    /** The crumb for a sign-in; a prefix keeps it from ever being Jenkins' own for some session. */
    static String bound(String signIn, String salt) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            md.update(("netci-shared-session;" + signIn).getBytes(StandardCharsets.UTF_8));
            return Util.toHexString(md.digest(salt.getBytes(StandardCharsets.US_ASCII)));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static boolean same(String a, String b) {
        return a != null && MessageDigest.isEqual(a.getBytes(StandardCharsets.US_ASCII), b.getBytes(StandardCharsets.US_ASCII));
    }

    /** After JCasC (which configures before SYSTEM_CONFIG_LOADED) and only over Jenkins' default. */
    @Initializer(after = InitMilestone.JOB_CONFIG_ADAPTED)
    public static void install() {
        Jenkins j = Jenkins.get();
        CrumbIssuer current = j.getCrumbIssuer();
        if (SharedSession.get() != null && current != null && current.getClass() == DefaultCrumbIssuer.class) {
            j.setCrumbIssuer(new SharedCrumbIssuer());
            LOGGER.info("netCI: CSRF crumbs are bound to the shared sign-in, so every replica accepts them");
        }
    }

    @Extension
    @Symbol("netciSharedSession")
    public static final class DescriptorImpl extends CrumbIssuerDescriptor<SharedCrumbIssuer> {
        public DescriptorImpl() {
            // The salt Jenkins' DefaultCrumbIssuer uses: the same confidential key, so the same
            // value on every replica (its secrets/ are shared, ADR-076 phase 5).
            super(new HexStringConfidentialKey(Jenkins.class, "crumbSalt", 16).get(),
                    SystemProperties.getString("hudson.security.csrf.requestfield", CrumbIssuer.DEFAULT_CRUMB_NAME));
        }

        @Override
        public String getDisplayName() {
            return "netCI: a crumb every replica accepts";
        }
    }
}
