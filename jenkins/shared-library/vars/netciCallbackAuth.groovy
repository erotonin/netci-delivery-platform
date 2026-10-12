// Run `body` with the credential netCI callbacks should use.
//
// netCI mints a token for every build and passes it as the masked build parameter
// NETCI_CALLBACK_TOKEN. That token can report for this run and nothing else, so it is
// what `scripts/netci_callback.py` prefers. The shared `netci-pipeline-api-key`
// credential is consulted only when no token was handed over -- local mode before
// workload identity is configured -- and does not have to exist otherwise.
def call(String callbackCredentialsId, Closure body) {
    if (env.NETCI_CALLBACK_TOKEN?.trim()) {
        body()
        return
    }
    withCredentials([string(credentialsId: callbackCredentialsId, variable: 'NETCI_PIPELINE_API_KEY')]) {
        body()
    }
}
