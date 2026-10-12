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
 */
def call(Closure body) {
    def name = env.NETCI_BUILD_CONTAINER?.trim()
    if (name) {
        container(name) { body() }
    } else {
        body()
    }
}
