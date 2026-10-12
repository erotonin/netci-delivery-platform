// SPDX-License-Identifier: MIT
// Prepared as a self-contained contribution candidate for jenkinsci/pipeline-examples.

def call(Map config = [:]) {
    def scriptDirectory = config.get('scriptDirectory', 'scripts/ci')
    def signingKeyCredentialId = config.get('signingKeyCredentialId', 'cosign-signing-key-file')
    def disposableAgent = config.get('disposableAgent', true) as boolean

    pipeline {
        agent { label config.get('agentLabel', 'ephemeral-build') }
        options {
            timestamps()
            skipDefaultCheckout(true)
            disableConcurrentBuilds()
            buildDiscarder(logRotator(numToKeepStr: '30'))
        }
        environment {
            CI_SCRIPT_DIR = "${scriptDirectory}"
            EVIDENCE_DIR = "${env.WORKSPACE}/.build-evidence"
        }
        stages {
            stage('Checkout') {
                steps {
                    checkout scm
                    sh 'mkdir -p "${EVIDENCE_DIR}"'
                }
            }
            stage('Test') {
                steps { sh 'bash "${CI_SCRIPT_DIR}/test.sh"' }
            }
            stage('Build') {
                steps { sh 'bash "${CI_SCRIPT_DIR}/build.sh"' }
            }
            stage('SBOM') {
                steps { sh 'bash "${CI_SCRIPT_DIR}/sbom.sh"' }
            }
            stage('Vulnerability scan') {
                steps { sh 'bash "${CI_SCRIPT_DIR}/scan.sh"' }
            }
            stage('Sign') {
                steps {
                    withCredentials([file(
                        credentialsId: signingKeyCredentialId,
                        variable: 'COSIGN_KEY_FILE',
                    )]) {
                        sh 'COSIGN_KEY_REF="${COSIGN_KEY_FILE}" bash "${CI_SCRIPT_DIR}/sign.sh"'
                    }
                }
            }
            stage('Publish') {
                steps { sh 'bash "${CI_SCRIPT_DIR}/publish.sh"' }
            }
        }
        post {
            always {
                dir("${env.EVIDENCE_DIR}") {
                    archiveArtifacts artifacts: '**', allowEmptyArchive: true
                }
            }
            cleanup {
                script {
                    if (disposableAgent) {
                        echo 'Workspace is destroyed with the disposable agent.'
                    } else {
                        cleanWs deleteDirs: true, disableDeferredWipeout: true
                    }
                }
            }
        }
    }
}
