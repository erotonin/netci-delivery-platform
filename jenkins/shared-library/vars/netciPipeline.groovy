def call(Map config = [:]) {
    def template = config.get('template', 'container-ci-cd-v1')
    def stages = config.get('stages', [
        'checkout', 'unit-test', 'build', 'sbom',
        'vulnerability-scan', 'sign', 'publish'
    ])
    def ciScriptDir = config.get('ciScriptDir', "templates/${template}/scripts/ci")

    pipeline {
        agent { label config.get('agentLabel', 'netci-ephemeral') }
        options {
            timestamps()
            skipDefaultCheckout(true)
            disableConcurrentBuilds()
            buildDiscarder(logRotator(numToKeepStr: '30'))
        }
        environment {
            NETCI_TEMPLATE = template
            NETCI_CI_SCRIPT_DIR = ciScriptDir
            NETCI_CORRELATION_ID = "${env.JOB_NAME}-${env.BUILD_NUMBER}"
        }
        stages {
            stage('Checkout') {
                steps {
                    container('builder') {
                        checkout scm
                    }
                }
            }
            stage('Unit Test') {
                when { expression { stages.contains('unit-test') } }
                steps {
                    container('builder') {
                        sh 'bash "${NETCI_CI_SCRIPT_DIR}/test.sh"'
                    }
                }
            }
            stage('Build') {
                when { expression { stages.contains('build') } }
                steps {
                    container('builder') {
                        sh 'bash "${NETCI_CI_SCRIPT_DIR}/build.sh"'
                    }
                }
            }
            stage('SBOM') {
                when { expression { stages.contains('sbom') } }
                steps {
                    container('builder') {
                        sh 'bash "${NETCI_CI_SCRIPT_DIR}/sbom.sh"'
                    }
                }
            }
            stage('Vulnerability Scan') {
                when { expression { stages.contains('vulnerability-scan') } }
                steps {
                    container('builder') {
                        sh 'bash "${NETCI_CI_SCRIPT_DIR}/scan.sh"'
                    }
                }
            }
            stage('Sign') {
                when { expression { stages.contains('sign') } }
                steps {
                    container('builder') {
                        sh 'bash "${NETCI_CI_SCRIPT_DIR}/sign.sh"'
                    }
                }
            }
            stage('Publish') {
                when { expression { stages.contains('publish') } }
                steps {
                    container('builder') {
                        sh 'bash "${NETCI_CI_SCRIPT_DIR}/publish.sh"'
                    }
                }
            }
        }
        post {
            always {
                archiveArtifacts artifacts: '**/sbom.json,**/scan-report.json,**/artifact-digest.txt,**/artifact-ref.txt,**/*.bundle.json', allowEmptyArchive: true
                cleanWs(deleteDirs: true, disableDeferredWipeout: true)
            }
        }
    }
}
