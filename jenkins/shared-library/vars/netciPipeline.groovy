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

    pipeline {
        agent { label params.NETCI_AGENT_LABEL ?: config.get('agentLabel', 'netci-ephemeral') }
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
                    netciInBuilder {
                        script {
                            if (params.GIT_URL?.trim()) {
                                checkout([
                                    $class: 'GitSCM',
                                    branches: [[name: params.COMMIT_SHA?.trim() ?: (params.GIT_BRANCH ?: 'main')]],
                                    userRemoteConfigs: [[url: params.GIT_URL]]
                                ])
                            } else {
                                checkout scm
                            }
                        }
                        sh 'mkdir -p "${NETCI_OUTPUT_DIR}"'
                    }
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
                          mkdir -p "${HOME}/.netci-cache"
                          if [ -f "${HOME}/.netci-cache/warm" ]; then
                            echo "NETCI_CACHE=hit"
                          else
                            echo "NETCI_CACHE=miss"
                            date -u +%FT%TZ > "${HOME}/.netci-cache/warm"
                          fi
                        '''
                    }
                }
            }
            stage('Unit Test') {
                when { expression { netciStageEnabled('unit-test', defaultStages) } }
                steps { netciInBuilder { sh 'bash "${NETCI_CI_SCRIPT_DIR}/test.sh"' } }
            }
            stage('Build') {
                when { expression { netciStageEnabled('build', defaultStages) } }
                steps { netciInBuilder { sh 'bash "${NETCI_CI_SCRIPT_DIR}/build.sh"' } }
            }
            stage('SBOM') {
                when { expression { netciStageEnabled('sbom', defaultStages) } }
                steps { netciInBuilder { sh 'bash "${NETCI_CI_SCRIPT_DIR}/sbom.sh"' } }
            }
            stage('Vulnerability Scan') {
                when { expression { netciStageEnabled('vulnerability-scan', defaultStages) } }
                steps { netciInBuilder { sh 'bash "${NETCI_CI_SCRIPT_DIR}/scan.sh"' } }
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
            stage('Publish') {
                when { expression { netciStageEnabled('publish', defaultStages) } }
                steps { netciInBuilder { sh 'bash "${NETCI_CI_SCRIPT_DIR}/publish.sh"' } }
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
                    dir("${env.NETCI_OUTPUT_DIR}") {
                        archiveArtifacts(artifacts: '**', allowEmptyArchive: true)
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

/** Honour the stage list netCI selected for this application, falling back to the template default. */
private boolean netciStageEnabled(String stageId, List defaultStages) {
    def declared = params.NETCI_STAGES?.trim()
    def stages = declared ? declared.split(',').collect { it.trim() } : defaultStages
    return stages.contains(stageId)
}
