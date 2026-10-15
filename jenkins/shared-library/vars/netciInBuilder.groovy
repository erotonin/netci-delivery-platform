/**
 * Run build steps in the right place for whichever agent this build landed on.
 *
 * On the ephemeral agent the work belongs in a specific container of the pod, and the
 * pod template names it in NETCI_BUILD_CONTAINER. A long-lived agent is a single process
 * with the same toolchain on PATH and no containers to switch into, where `container()`
 * fails outright with "Node is not a Kubernetes node".
 *
 * Both have to work: `scripts/gate_benchmark.py` measures the cost of the ephemeral agent
 * by running the *same* pipeline on a shared one, and a comparison between two different
 * pipeline definitions would measure the definitions instead of the agents.
 *
 * On a long-lived agent (a VM's, ADR-092) each build also gets its own image storage, under
 * the build's temporary directory: Buildah's default one, in the agent's home, kept every image
 * every build had made (233 MB after the first on the HA lab) until the VM's disk filled, and a
 * later build of another module could find this one's images -- a base image tag included --
 * already there. `netciInBuilder.dropStorage()` removes it when the build ends.
 */
def call(Closure body) {
    def name = env.NETCI_BUILD_CONTAINER?.trim()
    if (name) {
        container(name) { body() }
    } else {
        // Rootless Buildah reads its storage from $XDG_CONFIG_HOME/containers/storage.conf and
        // ignores CONTAINERS_STORAGE_CONF (measured, buildah 1.39): the toolbox keeps nothing
        // else in ~/.config/containers.
        withEnv(["XDG_CONFIG_HOME=${storageRoot()}/config"]) {
            sh(label: 'per-build image storage', script: '''
                conf="$XDG_CONFIG_HOME/containers/storage.conf"
                root="$(dirname "$XDG_CONFIG_HOME")"
                # The toolbox's own configuration (jenkins/agent-toolbox/Dockerfile) in another
                # place. ignore_chown_errors is what lets a layer unpack where newuidmap is denied
                # and the namespace has one id: without it the first base image failed
                # ("insufficient UIDs or GIDs ... requested 0:42 for /etc/shadow").
                if [ ! -s "$conf" ]; then
                    mkdir -p "$root/graph" "$root/run" "$XDG_CONFIG_HOME/containers" && chmod 700 "$root"
                    printf '[storage]\\ndriver = "%s"\\ngraphroot = "%s/graph"\\nrunroot = "%s/run"\\n[storage.options]\\nignore_chown_errors = "true"\\n[storage.options.vfs]\\nignore_chown_errors = "true"\\n' \\
                        "${STORAGE_DRIVER:-vfs}" "$root" "$root" > "$conf"
                fi
            ''')
            body()
        }
    }
}

/** The build's image storage, gone. Its files belong to the user namespace's ids, so it is
 *  removed from inside that namespace; best effort, as a failed cleanup must not fail a build. */
def dropStorage() {
    if (env.NETCI_BUILD_CONTAINER?.trim()) {
        return
    }
    withEnv(["NETCI_STORAGE_ROOT=${storageRoot()}"]) {
        sh(label: 'drop per-build image storage', returnStatus: true, script: '''
            [ -d "$NETCI_STORAGE_ROOT" ] || exit 0
            buildah unshare rm -rf "$NETCI_STORAGE_ROOT" || rm -rf "$NETCI_STORAGE_ROOT"
        ''')
    }
}

private String storageRoot() {
    return "${env.WORKSPACE_TMP ?: env.WORKSPACE + '@tmp'}/netci-containers"
}
