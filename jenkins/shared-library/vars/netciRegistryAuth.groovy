// Run `body` logged in to the registry builds push to, when a credential is configured
// (ADR-054).
//
// With no credential id -- the lab, whose registry is anonymous -- `body()` runs exactly
// as it always did. Otherwise a Username with password credential is bound for this one
// stage and turned into one auth file that every tool in it reads:
//
//   buildah  REGISTRY_AUTH_FILE (containers-auth.json)
//   cosign, syft, trivy  $DOCKER_CONFIG/config.json, through go-containerregistry's
//            default keychain (trivy also for its vulnerability-database mirror)
//
// They are the same file: containers-auth.json is the `auths` section of Docker's
// config.json. `buildah login` writes it, so the password goes in on stdin, is checked
// against the registry before anything is built on it, and a wrong one fails the stage
// here rather than as an "unauthorized" three stages later. The file is created under
// umask 077 beside the workspace -- never in it, where it would be build context and
// archived evidence -- and removed when the stage ends, whether or not it succeeded.
//
// A verify-only build (a fork's pull request, ADR-043) runs code nobody reviewed and
// pushes nothing, so it is never given the credential: netciPipeline passes an empty id
// for one, and this refuses outright if it is ever handed a real one.
def call(String credentialsId, Closure body) {
    if (!credentialsId?.trim()) {
        body()
        return
    }
    if (params.NETCI_PUBLISH?.trim() == 'false') {
        error('refusing to bind the registry credential in a verify-only build')
    }
    def authDir = "${env.WORKSPACE_TMP ?: env.WORKSPACE + '@tmp'}/netci-registry-auth"
    withCredentials([usernamePassword(credentialsId: credentialsId,
                                      usernameVariable: 'NETCI_REGISTRY_USERNAME',
                                      passwordVariable: 'NETCI_REGISTRY_PASSWORD')]) {
        withEnv(["DOCKER_CONFIG=${authDir}", "REGISTRY_AUTH_FILE=${authDir}/config.json"]) {
            try {
                // Only the push host: it is the one registry a build talks to (the base
                // image and the Trivy mirror live in it). The pull host is where clusters
                // pull from; no build step contacts it, and it need not even resolve here.
                sh(label: 'registry login', script: '''
                  set +x
                  set -eu
                  umask 077
                  rm -rf "${DOCKER_CONFIG}"
                  mkdir -p "${DOCKER_CONFIG}"
                  printf '%s' "${NETCI_REGISTRY_PASSWORD}" | buildah login \\
                    --authfile "${REGISTRY_AUTH_FILE}" \\
                    --tls-verify="${REGISTRY_TLS_VERIFY:-false}" \\
                    --username "${NETCI_REGISTRY_USERNAME}" \\
                    --password-stdin \\
                    "${REGISTRY_PUSH_HOST}"
                ''')
                body()
            } finally {
                sh(label: 'registry logout', script: 'rm -rf "${DOCKER_CONFIG}"')
            }
        }
    }
}
