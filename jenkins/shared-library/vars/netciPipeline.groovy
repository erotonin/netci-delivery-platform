/**
 * netCI standard CI pipeline.
 *
 * Jenkins owns build execution only. Every transition is reported back to netCI,
 * which owns application identity, policy and deployment. The build publishes the
 * SBOM, scan and signature evidence it produced; if netCI's supply-chain policy
 * denies the artifact, the build fails here rather than handing on an artifact
 * that could not be verified.
 */
def call(Map config = [:]) {
    def template = config.get('template', 'container-ci-cd-v1')
    def defaultStages = [
        'checkout', 'unit-test', 'build', 'sbom',
        'vulnerability-scan', 'sign', 'publish'
    ]
    // Relative to the tooling netciTooling() writes, unless a job overrides it.
    def ciScriptDirOverride = config.get('ciScriptDir', '')
    def callbackCredentialsId = config.get('callbackCredentialsId', 'netci-pipeline-api-key')
    def cosignCredentialsId = config.get('cosignCredentialsId', 'netci-cosign-key')
    // Optional, all empty in the lab (ADR-054): a Secret text with the cosign key's
    // password, a Username with password for the application's git server, and one for
    // the registry. Empty means what it always meant: no password, anonymous git,
    // anonymous registry.
    def cosignPasswordCredentialsId = config.get('cosignPasswordCredentialsId', '')?.trim() ?: ''
    def gitCredentialsId = config.get('gitCredentialsId', '')?.trim() ?: ''
    def gitToolName = config.get('gitToolName', 'Default')
    // A verify-only build runs a fork's unreviewed code and must not reach the registry
    // (ADR-043/054): it pushes nothing, so it is given no registry credential at all.
    def registryCredentialsId = netciVerifyOnly() ? '' : (config.get('registryCredentialsId', '')?.trim() ?: '')

    // Per-project isolation (ADR-030). netCI provisions a namespace, a service account
    // and a cache claim for the application and names them in the build parameters;
    // the pod for this build inherits the cloud's template but is created in *that*
    // namespace and mounts *that* cache. NETCI_AGENT_LABEL names another pod template
    // to inherit from instead (the benchmark's long-lived `netci-shared` baseline).
    def isolatedNamespace = params.NETCI_BUILD_NAMESPACE?.trim() ?: ''
    def agentServiceAccount = params.NETCI_BUILD_SERVICE_ACCOUNT?.trim() ?: ''
    def cacheClaim = isolatedNamespace ? (params.NETCI_BUILD_CACHE_CLAIM?.trim() ?: '') : ''
    def agentTemplate = params.NETCI_AGENT_LABEL?.trim() ?: config.get('agentLabel', 'netci-ephemeral')
    def podYaml = netciCachePodYaml(agentServiceAccount, cacheClaim)
    // The benchmark baseline keeps its pod between builds (a fixed label plus an idle
    // window is what lets the plugin hand the same pod to the next build). Everything
    // netCI dispatches gets a fresh pod: label generated, idle 0, retention never.
    def reusablePod = (agentTemplate == 'netci-shared')
    def podLabel = reusablePod ? 'netci-shared' : "netci-${env.JOB_NAME}-${env.BUILD_NUMBER}".replaceAll('[^A-Za-z0-9_-]', '-')
    def podIdleMinutes = reusablePod ? 120 : 0

    pipeline {
        agent {
            kubernetes {
                inheritFrom agentTemplate
                label podLabel
                idleMinutes podIdleMinutes
                namespace isolatedNamespace
                serviceAccount agentServiceAccount
                yaml podYaml
                yamlMergeStrategy merge()
                defaultContainer 'jnlp'
            }
        }
        options {
            timestamps()
            skipDefaultCheckout(true)
            disableConcurrentBuilds()
            buildDiscarder(logRotator(numToKeepStr: '30'))
        }
        environment {
            NETCI_TEMPLATE = "${params.NETCI_TEMPLATE ?: template}"
            NETCI_OUTPUT_DIR = "${env.WORKSPACE}/.netci-out"
            // netCI is the source of the correlation id; only fall back when a build
            // is started by hand from the Jenkins UI.
            NETCI_CORRELATION_ID = "${params.NETCI_CORRELATION_ID ?: env.JOB_NAME + '-' + env.BUILD_NUMBER}"
            NETCI_PIPELINE_RUN_ID = "${params.NETCI_PIPELINE_RUN_ID ?: ''}"
            NETCI_API_URL = "${params.NETCI_API_URL ?: ''}"
            IMAGE_TAG = "${params.COMMIT_SHA ?: env.BUILD_NUMBER}"
            // Supplied by netCI: a build agent has no route to a public registry, so
            // both the base image and the publish target come from the platform.
            NETCI_BASE_IMAGE = "${params.NETCI_BASE_IMAGE ?: ''}"
            REGISTRY_PUSH_HOST = "${params.REGISTRY_PUSH_HOST ?: 'netci-registry:5000'}"
            REGISTRY_PULL_HOST = "${params.REGISTRY_PULL_HOST ?: params.REGISTRY_PUSH_HOST ?: 'localhost:5000'}"
            TRIVY_DB_REPOSITORY = "${params.NETCI_TRIVY_DB_REPOSITORY ?: ''}"
            // netCI always sends both (ADR-054). Empty -- a build started by hand, or by a
            // netCI that predates them -- leaves the scripts' own defaults in force.
            REGISTRY_TLS_VERIFY = "${params.REGISTRY_TLS_VERIFY ?: ''}"
            COSIGN_TLOG_UPLOAD = "${params.COSIGN_TLOG_UPLOAD ?: ''}"
            COSIGN_REKOR_URL = "${params.COSIGN_REKOR_URL ?: ''}"
            // Syft phones home for a version check that a build farm cannot reach and
            // does not need; the check timing out would otherwise add 30s per build.
            SYFT_CHECK_FOR_APP_UPDATE = "false"
        }
        stages {
            stage('Checkout') {
                steps {
                    script {
                        def tooling = netciTooling()
                        env.NETCI_CI_SCRIPT_DIR = ciScriptDirOverride ?: "${tooling}/templates/${env.NETCI_TEMPLATE}/scripts/ci"
                        // What to build and what to call it, both from netCI. They never
                        // reached the build before, so the CI scripts fell back to
                        // sample-apps/hello-container and every container module in the lab
                        // built and pushed that one sample app. Set here rather than in
                        def rawAppDir = params.NETCI_APP_DIR?.trim()
                        def resolvedAppDir = rawAppDir ?: '.'
                        if ((!rawAppDir || rawAppDir == '.') && !fileExists('Dockerfile')) {
                            def candidate = "sample-apps/${params.NETCI_IMAGE_NAME ?: ''}"
                            if (fileExists("${candidate}/Dockerfile")) {
                                resolvedAppDir = candidate
                            }
                        }
                        env.NETCI_APP_DIR = "${env.WORKSPACE}/${resolvedAppDir}"
                        env.NETCI_IMAGE_NAME = params.NETCI_IMAGE_NAME?.trim() ?: ''
                        // A build netCI dispatched always says what it is building. Without
                        // a name the scripts would publish under their sample default --
                        // exactly the defect this replaces -- so refuse instead.
                        if (env.NETCI_PIPELINE_RUN_ID?.trim() && !env.NETCI_IMAGE_NAME?.trim()) {
                            error('NETCI_IMAGE_NAME was not supplied: this netCI predates it, or the job was edited by hand')
                        }
                    }
                    // On the agent's own container, not through `container()`: the git
                    // plugin runs ~30 git commands for one checkout and each exec into
                    // the builder costs ~0.4 s of round trip (JENKINS-30600). Measured:
                    // 18-20 s of checkout for a 4 MB repository, all of it overhead. The
                    // workspace volume is shared, so the builder sees the result.
                    script {
                        if (params.GIT_URL?.trim()) {
                            // netCI names the exact commit. With a project cache the source
                            // is materialised from the project's bare mirror with one
                            // `git archive` -- no working clone, no `.git`, therefore no
                            // hooks or config a previous build could have planted, and none
                            // of the ~30 git invocations the git plugin makes per checkout
                            // (measured at 17 s of a 40 s build). The mirror is refreshed
                            // first, so a stale cache can never pin an old commit; if the
                            // commit is not in the mirror after the refresh, the build fails
                            // rather than building something else.
                            def commit = params.COMMIT_SHA?.trim() ?: ''
                            // A fork's pull request commit is on no branch of this
                            // repository; only its PR ref reaches it. netCI sends one of
                            // two shapes and this checks them again, because the value
                            // lands in a refspec.
                            def prRef = params.NETCI_GIT_REF?.trim() ?: ''
                            if (prRef && !(prRef ==~ /refs\/(pull|merge-requests)\/[0-9]{1,9}\/head/)) {
                                error("NETCI_GIT_REF is not a pull request ref: ${prRef}")
                            }
                            def prRefspec = prRef ? " '+${prRef}:${prRef}'" : ''
                            if (params.NETCI_BUILD_CACHE_CLAIM?.trim() && fileExists('/netci-cache') && commit ==~ /[0-9a-f]{40}/) {
                                def mirror = '/netci-cache/git/mirror.git'
                                // With a credential, the git plugin's binding hands git the
                                // password through GIT_ASKPASS for these commands only; it is
                                // never in the URL, so never in the mirror's config. An empty
                                // credential.helper stops a helper on the agent image from
                                // storing it where the next build could read it.
                                def git = gitCredentialsId ? 'git -c credential.helper=' : 'git'
                                def refreshMirror = {
                                    sh """
                                      set -eu
                                      if [ -d '${mirror}' ]; then
                                        git -C '${mirror}' remote set-url origin '${params.GIT_URL}'
                                        ${git} -C '${mirror}' fetch --prune origin '+refs/heads/*:refs/heads/*'${prRefspec} || { rm -rf '${mirror}'; ${git} clone --mirror '${params.GIT_URL}' '${mirror}'; }
                                      else
                                        mkdir -p /netci-cache/git
                                        ${git} clone --mirror '${params.GIT_URL}' '${mirror}'
                                      fi
                                    """
                                }
                                if (gitCredentialsId) {
                                    withCredentials([gitUsernamePassword(credentialsId: gitCredentialsId, gitToolName: gitToolName)]) {
                                        refreshMirror()
                                    }
                                } else {
                                    refreshMirror()
                                }
                                // Everything after the refresh is local to the mirror.
                                sh """
                                  set -eu
                                  git -C '${mirror}' cat-file -e '${commit}^{commit}' || { echo "commit ${commit} is not in ${params.GIT_URL}" >&2; exit 1; }
                                  find . -mindepth 1 -maxdepth 1 -exec rm -rf {} +
                                  git -C '${mirror}' archive --format=tar '${commit}' | tar -x
                                  printf '%s\n' '${commit}' > .netci-commit
                                """
                                env.GIT_COMMIT = commit
                            } else {
                                def remote = [url: params.GIT_URL]
                                if (gitCredentialsId) {
                                    // The git plugin passes it through its own askpass helper.
                                    remote.credentialsId = gitCredentialsId
                                }
                                if (prRef) {
                                    remote.refspec = "+refs/heads/*:refs/remotes/origin/* +${prRef}:refs/remotes/origin/netci-pr-head"
                                }
                                checkout([
                                    $class: 'GitSCM',
                                    branches: [[name: commit ?: (params.GIT_BRANCH ?: 'main')]],
                                    userRemoteConfigs: [remote]
                                ])
                            }
                        } else {
                            checkout scm
                        }
                    }
                    sh 'mkdir -p "${NETCI_OUTPUT_DIR}"'
                }
            }
            // Reported after checkout: the start and the checkout result go out together.
            stage('Report Start') {
                when { expression { env.NETCI_PIPELINE_RUN_ID?.trim() && env.NETCI_API_URL?.trim() } }
                steps {
                    netciInBuilder {
                        netciCallbackAuth(callbackCredentialsId) {
                            sh 'mkdir -p "${NETCI_OUTPUT_DIR}"'
                            sh 'python3 "${NETCI_TOOLING_DIR}/scripts/netci_callback.py" status --status running --log "jenkins build ${BUILD_TAG} started"'
                            // Checkout itself finished before the callback script existed;
                            // its result is reported here, once, after the fact.
                            sh 'python3 "${NETCI_TOOLING_DIR}/scripts/netci_callback.py" stage --id checkout --name Checkout --status succeeded >/dev/null'
                        }
                    }
                }
            }
            stage('Prepare Cache') {
                steps {
                    netciInBuilder {
                        // A shared agent keeps this directory between builds; an ephemeral
                        // pod gets a fresh emptyDir every time. Printing hit or miss is what
                        // makes the benchmark's cache column a measurement rather than a guess.
                        sh '''
                          cache_dir="${NETCI_CACHE_DIR:-${HOME}/.netci-cache}"
                          mkdir -p "${cache_dir}"
                          if [ -f "${cache_dir}/warm" ]; then
                            echo "NETCI_CACHE=hit"
                          else
                            echo "NETCI_CACHE=miss"
                            date -u +%FT%TZ > "${cache_dir}/warm"
                          fi
                          echo "NETCI_CACHE_DIR=${cache_dir}"
                          du -sh "${cache_dir}" 2>/dev/null || true
                        '''
                    }
                }
            }
            stage('Custom: after checkout') {
                when { expression { netciCustomStagesAfter('checkout') } }
                steps { script { netciRunCustomStages('checkout') } }
            }
            stage('Unit Test') {
                when { expression { netciStageEnabled('unit-test', defaultStages) } }
                steps { script { netciStage('unit-test', 'Unit Test', callbackCredentialsId) { netciInBuilder { sh 'bash "${NETCI_CI_SCRIPT_DIR}/test.sh"'} } } }

            }
            stage('Custom: after unit-test') {
                when { expression { netciCustomStagesAfter('unit-test') } }
                steps { script { netciRunCustomStages('unit-test') } }
            }
            stage('Build') {
                when { expression { netciStageEnabled('build', defaultStages) } }
                // The registry credential is bound in the stages that talk to the registry
                // -- Build pulls the base image -- and in no other.
                steps { script { netciStage('build', 'Build', callbackCredentialsId) { netciInBuilder { netciRegistryAuth(registryCredentialsId) { sh 'bash "${NETCI_CI_SCRIPT_DIR}/build.sh"' } } } } }

            }
            stage('Custom: after build') {
                when { expression { netciCustomStagesAfter('build') } }
                steps { script { netciRunCustomStages('build') } }
            }
            stage('SBOM') {
                when { expression { netciStageEnabled('sbom', defaultStages) } }
                steps { script { netciStage('sbom', 'Generate SBOM', callbackCredentialsId) { netciInBuilder { netciRegistryAuth(registryCredentialsId) { sh 'bash "${NETCI_CI_SCRIPT_DIR}/sbom.sh"' } } } } }

            }
            stage('Custom: after sbom') {
                when { expression { netciCustomStagesAfter('sbom') } }
                steps { script { netciRunCustomStages('sbom') } }
            }
            stage('Vulnerability Scan') {
                when { expression { netciStageEnabled('vulnerability-scan', defaultStages) } }
                steps { script { netciStage('vulnerability-scan', 'Vulnerability Scan', callbackCredentialsId) { netciInBuilder { netciRegistryAuth(registryCredentialsId) { sh 'bash "${NETCI_CI_SCRIPT_DIR}/scan.sh"' } } } } }

            }
            stage('Custom: after vulnerability-scan') {
                when { expression { netciCustomStagesAfter('vulnerability-scan') } }
                steps { script { netciRunCustomStages('vulnerability-scan') } }
            }
            stage('Sign') {
                when { expression { netciStageEnabled('sign', defaultStages) } }
                steps { script { netciStage('sign', 'Sign Artifact', callbackCredentialsId) {
                    netciInBuilder {
                        // The signing key is written beside the workspace, never in it, and
                        // removed on every exit. It used to be written to NETCI_OUTPUT_DIR and
                        // removed only after sign.sh succeeded: a failed Sign left the key
                        // there for post/always to archive as a build artifact. It is never
                        // passed on a command line, where it would appear in the process
                        // table and the build log; neither is its password.
                        def signingCredentials = [string(credentialsId: cosignCredentialsId, variable: 'NETCI_COSIGN_PRIVATE_KEY')]
                        if (cosignPasswordCredentialsId) {
                            signingCredentials << string(credentialsId: cosignPasswordCredentialsId, variable: 'NETCI_COSIGN_PASSWORD')
                        }
                        netciRegistryAuth(registryCredentialsId) {
                            withCredentials(signingCredentials) {
                                sh '''
                                  set +x
                                  set -eu
                                  umask 077
                                  key_dir="${WORKSPACE_TMP:-${WORKSPACE}@tmp}/netci-signing"
                                  trap 'rm -rf "${key_dir}"' EXIT
                                  mkdir -p "${key_dir}"
                                  printf '%s' "${NETCI_COSIGN_PRIVATE_KEY}" > "${key_dir}/cosign.key"
                                  COSIGN_KEY_REF="${key_dir}/cosign.key" \\
                                  COSIGN_PASSWORD="${NETCI_COSIGN_PASSWORD:-}" \\
                                    bash "${NETCI_CI_SCRIPT_DIR}/sign.sh"
                                '''
                            }
                        }
                    }
                } } }
            }
            stage('Custom: after sign') {
                when { expression { netciCustomStagesAfter('sign') } }
                steps { script { netciRunCustomStages('sign') } }
            }
            stage('Publish') {
                when { expression { netciStageEnabled('publish', defaultStages) } }
                steps { script { netciStage('publish', 'Publish Artifact', callbackCredentialsId) { netciInBuilder { netciRegistryAuth(registryCredentialsId) { sh 'bash "${NETCI_CI_SCRIPT_DIR}/publish.sh"' } } } } }

            }
            stage('Custom: after publish') {
                when { expression { netciCustomStagesAfter('publish') } }
                steps { script { netciRunCustomStages('publish') } }
            }
            stage('Publish Evidence') {
                when { expression { env.NETCI_PIPELINE_RUN_ID?.trim() && env.NETCI_API_URL?.trim() && !netciVerifyOnly() } }
                steps {
                    netciInBuilder {
                        netciCallbackAuth(callbackCredentialsId) {
                            // Exits non-zero when netCI denies the artifact, so a
                            // policy failure fails the build instead of being logged.
                            sh 'python3 "${NETCI_TOOLING_DIR}/scripts/netci_callback.py" evidence'
                        }
                    }
                }
            }
        }
        post {
            always {
                netciInBuilder {
                    // Each stage removes its own on the way out; this catches a build that
                    // was killed between the write and the removal, on an agent that is
                    // kept for the next build.
                    sh(label: 'remove build credentials',
                       script: 'rm -rf "${WORKSPACE_TMP:-${WORKSPACE}@tmp}/netci-registry-auth" "${WORKSPACE_TMP:-${WORKSPACE}@tmp}/netci-signing"')
                    // Scoped with dir() rather than archived as '.netci-out/**' from the
                    // workspace root: the pattern makes Jenkins walk the entire workspace
                    // over the agent channel, .git included, to find a handful of evidence
                    // files. On an ephemeral agent that walk measured ~11s per build --
                    // more than pod provisioning and the build itself combined.
                    // The OCI archive is a build intermediate (syft/trivy read it); the
                    // registry holds the artifact, by digest. Shipping tens of MB over
                    // the agent channel to keep a copy Jenkins never serves was most of
                    // the post-build time on an ephemeral agent.
                    dir("${env.NETCI_OUTPUT_DIR}") {
                        // No key file is written here any more; excluding one anyway keeps a
                        // script that writes one from publishing it with the evidence.
                        archiveArtifacts(artifacts: '**', excludes: '*.oci.tar,**/*.key', allowEmptyArchive: true)
                    }
                }
            }
            success {
                script {
                    if (env.NETCI_PIPELINE_RUN_ID?.trim() && env.NETCI_API_URL?.trim()) {
                        netciInBuilder {
                            netciCallbackAuth(callbackCredentialsId) {
                                sh 'python3 "${NETCI_TOOLING_DIR}/scripts/netci_callback.py" status --status succeeded --log "jenkins build ${BUILD_TAG} succeeded"'
                            }
                        }
                    }
                }
            }
            unsuccessful {
                script {
                    if (env.NETCI_PIPELINE_RUN_ID?.trim() && env.NETCI_API_URL?.trim()) {
                        netciInBuilder {
                            netciCallbackAuth(callbackCredentialsId) {
                                // Best effort: a failed build must not be hidden by a
                                // failing callback, but netCI must still learn about it.
                                sh(script: 'python3 "${NETCI_TOOLING_DIR}/scripts/netci_callback.py" status --status failed --log "jenkins build ${BUILD_TAG} failed"', returnStatus: true)
                            }
                        }
                    }
                }
            }
            cleanup {
                script {
                    // On a reusable agent the wipe *is* the isolation: the next build must
                    // not see this one's workspace. On a disposable agent the pod and its
                    // emptyDir are deleted regardless, so wiping first buys nothing -- the
                    // pod template says which kind of agent this is.
                    if (env.NETCI_AGENT_DISPOSABLE?.trim() == 'true') {
                        echo 'workspace is destroyed with the agent; skipping cleanWs'
                    } else {
                        netciInBuilder {
                            cleanWs(deleteDirs: true, disableDeferredWipeout: true)
                        }
                    }
                }
            }
        }
    }
}

/** The pod fragment that gives a build its project's persistent cache. */
private String netciCachePodYaml(String serviceAccount, String cacheClaim) {
    def account = serviceAccount ? "  serviceAccountName: ${serviceAccount}\n" : ''
    if (!cacheClaim) {
        return "apiVersion: v1\nkind: Pod\nspec:\n${account}  restartPolicy: Never\n"
    }
    // One mount, one path, in both containers: the checkout runs on jnlp and keeps the
    // git mirror there; the builder keeps image layers and language caches there.
    return """
apiVersion: v1
kind: Pod
spec:
${account}  volumes:
    - name: netci-cache
      persistentVolumeClaim:
        claimName: ${cacheClaim}
  containers:
    - name: jnlp
      volumeMounts:
        - name: netci-cache
          mountPath: /netci-cache
    - name: builder
      env:
        - name: NETCI_CACHE_DIR
          value: /netci-cache
        - name: XDG_DATA_HOME
          value: /netci-cache/xdg
        - name: NETCI_BUILDAH_LAYERS
          value: "true"
        - name: GOCACHE
          value: /netci-cache/go-build
        - name: GOMODCACHE
          value: /netci-cache/go-mod
        - name: PIP_CACHE_DIR
          value: /netci-cache/pip
      volumeMounts:
        - name: netci-cache
          mountPath: /netci-cache
"""
}


/**
 * Custom catalog stages (ADR-030): registered by a platform administrator in netCI,
 * each one a script inside the repository anchored after a built-in stage. netCI
 * passes the ones this run selected as JSON; nothing here accepts a command.
 */
private List netciCustomStagesAfter(String anchor) {
    def raw = params.NETCI_CUSTOM_STAGES?.trim()
    if (!raw) { return [] }
    def entries = readJSON(text: raw)
    return entries.findAll { it.after == anchor && (it.script || it.code) }
}

private void netciRunCustomStages(String anchor) {
    netciCustomStagesAfter(anchor).each { entry ->
        // The path was validated by netCI (repository-relative, no traversal, *.sh) and
        // must exist in the checked-out commit; a stage whose script is missing fails
        // the build rather than being skipped, because a skipped gate is a false green.
        // Declared parameters arrive as environment variables; netCI validated the
        // names and values (no shell metacharacters), and nothing here interpolates them.
        def environment = (entry.env ?: [:]).collect { k, v -> "${k}=${v}" }
        stage(entry.name ?: entry.id) {
            netciInBuilder {
                if (entry.code) {
                    // A block of a shared pipeline (ADR-058): its code comes from the version
                    // netCI approved, not from the repository. Written beside the workspace
                    // and decoded by the shell, so no part of it is ever Groovy; run with no
                    // credential bound, like every author block.
                    if (!(entry.id ==~ /^[a-z][a-z0-9-]{1,40}$/)) {
                        error("custom stage id '${entry.id}' is not one netCI issues")
                    }
                    def dir = "${env.WORKSPACE_TMP ?: env.WORKSPACE + '@tmp'}/netci-blocks"
                    writeFile(file: "${dir}/${entry.id}.b64", text: entry.code)
                    withEnv(environment + ["NETCI_BLOCK=${dir}/${entry.id}"]) {
                        sh '''
                          set -eu
                          base64 -d "${NETCI_BLOCK}.b64" > "${NETCI_BLOCK}.sh"
                          bash "${NETCI_BLOCK}.sh"
                        '''
                    }
                    return
                }
                if (!fileExists(entry.script)) {
                    error("custom stage '${entry.id}' names ${entry.script}, which is not in this commit")
                }
                withEnv(environment) {
                    sh "bash '${entry.script}'"
                }
            }
        }
    }
}

/** Honour the stage list netCI selected for this application, falling back to the template default. */
private boolean netciStageEnabled(String stageId, List defaultStages) {
    // A verify-only build (a fork's pull request, ADR-043) is tested and scanned but never
    // signed or published: the signing key is not bound in it and nothing it produced can
    // be deployed. netCI refuses a digest from such a run, so skipping here is not the
    // only guard -- it is the one that keeps the key out of the build.
    if (netciVerifyOnly() && (stageId == 'sign' || stageId == 'publish')) {
        return false
    }
    def declared = params.NETCI_STAGES?.trim()
    def stages = declared ? declared.split(',').collect { it.trim() } : defaultStages
    return stages.contains(stageId)
}

private boolean netciVerifyOnly() {
    return params.NETCI_PUBLISH?.trim() == 'false'
}
