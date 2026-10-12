package hudson.remoting;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;

/**
 * Lays jars out as a remoting jar cache: each under the name the agent asks its cache for. The
 * names come from remoting's own Checksum and FileSystemJarCache (package-private, hence this
 * package), run against the controller's remoting, the side that names the jars it sends. An agent
 * loads a cached jar by its name without checking it, so the names must be exactly these.
 *
 * <pre>java -cp remoting.jar:. hudson.remoting.NetciAgentJarCache CACHE_DIR JAR...</pre>
 */
public final class NetciAgentJarCache {
    private NetciAgentJarCache() {}

    public static void main(String[] args) throws IOException {
        if (args.length < 2) {
            throw new IllegalArgumentException("usage: NetciAgentJarCache CACHE_DIR JAR...");
        }
        FileSystemJarCache cache = new FileSystemJarCache(new File(args[0]), false);
        for (int i = 1; i < args.length; i++) {
            File jar = new File(args[i]);
            Checksum sum = Checksum.forFile(jar);
            File to = cache.map(sum.sum1, sum.sum2);
            Files.createDirectories(to.getParentFile().toPath());
            Files.copy(jar.toPath(), to.toPath());
            System.out.println(cache.rootDir.toPath().relativize(to.toPath()) + " " + jar.getName());
        }
    }
}
