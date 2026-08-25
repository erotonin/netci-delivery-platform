def call(Map config = [:]) {
    def template = config.get('template', 'container-ci-cd-v1')
    def stages = config.get('stages', [
        'checkout', 'unit-test', 'build', 'sbom',
        'vulnerability-scan', 'sign', 'publish'
    ])

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
            NETCI_CORRELATION_ID = "${env.JOB_NAME}-${env.BUILD_NUMBER}"
        }
        stages {
            stage('Checkout') {
                steps {
                    checkout scm
                }
            }
            stage('Unit Test') {
                when { expression { stages.contains('unit-test') } }
                steps { sh './scripts/ci/test.sh' }
            }
            stage('Build') {
                when { expression { stages.contains('build') } }
                steps { sh './scripts/ci/build.sh' }
            }
            stage('SBOM') {
                when { expression { stages.contains('sbom') } }
                steps { sh 'syft . -o cyclonedx-json=sbom.json' }
            }
            stage('Vulnerability Scan') {
                when { expression { stages.contains('vulnerability-scan') } }
                steps { sh 'trivy fs --exit-code 1 --severity HIGH,CRITICAL .' }
            }
            stage('Sign') {
                when { expression { stages.contains('sign') } }
                steps { sh './scripts/ci/sign.sh' }
            }
            stage('Publish') {
                when { expression { stages.contains('publish') } }
                steps { sh './scripts/ci/publish.sh' }
            }
        }
        post {
            always {
                archiveArtifacts artifacts: '**/sbom.json,**/scan-report.json', allowEmptyArchive: true
                cleanWs(deleteDirs: true, disableDeferredWipeout: true)
            }
        }
    }
}
