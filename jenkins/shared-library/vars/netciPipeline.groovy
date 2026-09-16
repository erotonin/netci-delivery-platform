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
    def ciScriptDir = config.get('ciScriptDir', "templates/${template}/scripts/ci")
    def callbackCredentialsId = config.get('callbackCredentialsId', 'netci-pipeline-api-key')
    def cosignCredentialsId = config.get('cosignCredentialsId', 'netci-cosign-key')

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
            NETCI_CI_SCRIPT_DIR = "${ciScriptDir}"
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
            // Syft phones home for a version check that a build farm cannot reach and
            // does not need; the check timing out would otherwise add 30s per build.
            SYFT_CHECK_FOR_APP_UPDATE = "false"
        }
        stages {
            stage('Checkout') {
                steps {
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
                            if (params.NETCI_BUILD_CACHE_CLAIM?.trim() && fileExists('/netci-cache') && commit ==~ /[0-9a-f]{40}/) {
                                def mirror = '/netci-cache/git/mirror.git'
                                sh """
                                  set -eu
                                  if [ -d '${mirror}' ]; then
                                    git -C '${mirror}' remote set-url origin '${params.GIT_URL}'
                                    git -C '${mirror}' fetch --prune origin '+refs/heads/*:refs/heads/*' || { rm -rf '${mirror}'; git clone --mirror '${params.GIT_URL}' '${mirror}'; }
                                  else
                                    mkdir -p /netci-cache/git
                                    git clone --mirror '${params.GIT_URL}' '${mirror}'
                                  fi
                                  git -C '${mirror}' cat-file -e '${commit}^{commit}' || { echo "commit ${commit} is not in ${params.GIT_URL}" >&2; exit 1; }
                                  find . -mindepth 1 -maxdepth 1 -exec rm -rf {} +
                                  git -C '${mirror}' archive --format=tar '${commit}' | tar -x
                                  printf '%s\n' '${commit}' > .netci-commit
                                """
                                env.GIT_COMMIT = commit
                            } else {
                                checkout([
                                    $class: 'GitSCM',
                                    branches: [[name: commit ?: (params.GIT_BRANCH ?: 'main')]],
                                    userRemoteConfigs: [[url: params.GIT_URL]]
                                ])
                            }
                        } else {
                            checkout scm
                        }
                    }
                    sh 'mkdir -p "${NETCI_OUTPUT_DIR}"'
                }
            }
            // Reported after checkout, not before: netci_callback.py ships in the
            // repository, so there is nothing to report with until the agent has it.
            stage('Report Start') {
                when { expression { env.NETCI_PIPELINE_RUN_ID?.trim() && env.NETCI_API_URL?.trim() } }
                steps {
                    netciInBuilder {
                        netciCallbackAuth(callbackCredentialsId) {
                            sh 'mkdir -p "${NETCI_OUTPUT_DIR}"'
                            sh 'python3 scripts/netci_callback.py status --status running --log "jenkins build ${BUILD_TAG} started"'
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
                steps { netciInBuilder { sh 'bash "${NETCI_CI_SCRIPT_DIR}/test.sh"' } }
            }
            stage('Custom: after unit-test') {
                when { expression { netciCustomStagesAfter('unit-test') } }
                steps { script { netciRunCustomStages('unit-test') } }
            }
            stage('Build') {
                when { expression { netciStageEnabled('build', defaultStages) } }
                steps { netciInBuilder { sh 'bash "${NETCI_CI_SCRIPT_DIR}/build.sh"' } }
            }
            stage('Custom: after build') {
                when { expression { netciCustomStagesAfter('build') } }
                steps { script { netciRunCustomStages('build') } }
            }
            stage('SBOM') {
                when { expression { netciStageEnabled('sbom', defaultStages) } }
                steps { netciInBuilder { sh 'bash "${NETCI_CI_SCRIPT_DIR}/sbom.sh"' } }
            }
            stage('Custom: after sbom') {
                when { expression { netciCustomStagesAfter('sbom') } }
                steps { script { netciRunCustomStages('sbom') } }
            }
            stage('Vulnerability Scan') {
                when { expression { netciStageEnabled('vulnerability-scan', defaultStages) } }
                steps { netciInBuilder { sh 'bash "${NETCI_CI_SCRIPT_DIR}/scan.sh"' } }
            }
            stage('Custom: after vulnerability-scan') {
                when { expression { netciCustomStagesAfter('vulnerability-scan') } }
                steps { script { netciRunCustomStages('vulnerability-scan') } }
            }
            stage('Sign') {
                when { expression { netciStageEnabled('sign', defaultStages) } }
                steps {
                    netciInBuilder {
                        // The signing key is written to the ephemeral workspace and dies
                        // with the pod. It is never passed on a command line, where it
                        // would appear in the process table and the build log.
                        withCredentials([string(credentialsId: cosignCredentialsId, variable: 'NETCI_COSIGN_PRIVATE_KEY')]) {
                            sh '''
                              set -eu
                              umask 077
                              printf '%s' "${NETCI_COSIGN_PRIVATE_KEY}" > "${NETCI_OUTPUT_DIR}/cosign.key"
                              COSIGN_KEY_REF="${NETCI_OUTPUT_DIR}/cosign.key" \\
                              COSIGN_PASSWORD="" \\
                                bash "${NETCI_CI_SCRIPT_DIR}/sign.sh"
                              rm -f "${NETCI_OUTPUT_DIR}/cosign.key"
                            '''
                        }
                    }
                }
            }
            stage('Custom: after sign') {
                when { expression { netciCustomStagesAfter('sign') } }
                steps { script { netciRunCustomStages('sign') } }
            }
            stage('Publish') {
                when { expression { netciStageEnabled('publish', defaultStages) } }
                steps { netciInBuilder { sh 'bash "${NETCI_CI_SCRIPT_DIR}/publish.sh"' } }
            }
            stage('Custom: after publish') {
                when { expression { netciCustomStagesAfter('publish') } }
                steps { script { netciRunCustomStages('publish') } }
            }
            stage('Publish Evidence') {
                when { expression { env.NETCI_PIPELINE_RUN_ID?.trim() && env.NETCI_API_URL?.trim() } }
                steps {
                    netciInBuilder {
                        netciCallbackAuth(callbackCredentialsId) {
                            // Exits non-zero when netCI denies the artifact, so a
                            // policy failure fails the build instead of being logged.
                            sh 'python3 scripts/netci_callback.py evidence'
                        }
                    }
                }
            }
        }
        post {
            always {
                netciInBuilder {
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
                        archiveArtifacts(artifacts: '**', excludes: '*.oci.tar', allowEmptyArchive: true)
                    }
                }
            }
            success {
                script {
                    if (env.NETCI_PIPELINE_RUN_ID?.trim() && env.NETCI_API_URL?.trim()) {
                        netciInBuilder {
                            netciCallbackAuth(callbackCredentialsId) {
                                sh 'python3 scripts/netci_callback.py status --status succeeded --log "jenkins build ${BUILD_TAG} succeeded"'
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
                                sh(script: 'python3 scripts/netci_callback.py status --status failed --log "jenkins build ${BUILD_TAG} failed"', returnStatus: true)
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
    return entries.findAll { it.after == anchor && it.script }
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
    def declared = params.NETCI_STAGES?.trim()
    def stages = declared ? declared.split(',').collect { it.trim() } : defaultStages
    return stages.contains(stageId)
}
