// ---------------------------------------------------------------------------
// ZeroDownPipeline - build, verify, ship, and recover without a human.
//
// The pipeline is arranged so that the risky steps come last and the cheap
// checks come first: lint and tests fail in seconds and never touch AWS, the
// image is only built once it is worth building, and nothing reaches the
// instance until it has passed everything before it.
//
// The deploy stage cannot leave the system broken. deploy.py starts the new
// version on the INACTIVE colour and only moves traffic after a health check
// passes; if it fails, the new container is destroyed and the old one keeps
// serving. The post-deploy smoke test is the second line of defence, for a
// build that starts cleanly but is wrong - that one triggers rollback.py.
//
// Credentials this job expects in Jenkins:
//   dockerhub-credentials  Username/password  Docker Hub user + access token
//   ec2-ssh-key            SSH private key    the .pem from provision.py
//   aws-credentials        Username/password  AWS key id (user) + secret (pass)
// ---------------------------------------------------------------------------

pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()          // two deploys at once would fight over the colours
        buildDiscarder(logRotator(numToKeepStr: '20'))
        timeout(time: 25, unit: 'MINUTES')
    }

    environment {
        DOCKERHUB_USER = 'REPLACE_WITH_YOUR_DOCKERHUB_USER'
        DOCKER_IMAGE   = 'zerodownpipeline-api'
        AWS_REGION     = 'ap-south-1'
        S3_BUCKET      = 'REPLACE_WITH_YOUR_BUCKET'
        EC2_USER       = 'ec2-user'
        VENV           = "${WORKSPACE}/.venv"
    }

    parameters {
        booleanParam(
            name: 'ROLLBACK_DRILL',
            defaultValue: false,
            description: 'Deploy with a deliberately failing health check, to demonstrate automatic rollback. The build will end as UNSTABLE and the previous version stays live.'
        )
        booleanParam(
            name: 'SKIP_TESTS',
            defaultValue: false,
            description: 'Emergency escape hatch. Leave this off.'
        )
    }

    stages {

        stage('Checkout') {
            steps {
                checkout scm
                script {
                    // The git short SHA is the single identifier used everywhere:
                    // image tag, APP_VERSION inside the container, /health output,
                    // and the S3 ledger. One build is traceable end to end by it.
                    env.GIT_SHA = sh(returnStdout: true, script: 'git rev-parse --short HEAD').trim()
                    env.IMAGE_TAG = env.GIT_SHA
                    env.IMAGE_REF = "${env.DOCKERHUB_USER}/${env.DOCKER_IMAGE}:${env.IMAGE_TAG}"
                    currentBuild.displayName = "#${BUILD_NUMBER} ${env.GIT_SHA}"
                    currentBuild.description = params.ROLLBACK_DRILL ? 'rollback drill' : env.IMAGE_REF
                }
                sh 'echo "Building commit ${GIT_SHA}"'
            }
        }

        stage('Setup') {
            steps {
                sh '''
                    set -eux
                    python3 -m venv "${VENV}"
                    "${VENV}/bin/pip" install --quiet --upgrade pip
                    "${VENV}/bin/pip" install --quiet -r app/requirements-dev.txt
                '''
            }
        }

        stage('Lint') {
            steps {
                sh '"${VENV}/bin/flake8" app deploy infra'
            }
        }

        stage('Test') {
            when { expression { !params.SKIP_TESTS } }
            steps {
                sh '"${VENV}/bin/pytest" -v --junitxml=test-results.xml'
            }
            post {
                always {
                    junit allowEmptyResults: true, testResults: 'test-results.xml'
                }
            }
        }

        stage('Shell lint') {
            steps {
                // Every deploy action on the instance is a bash script, so a
                // syntax error here would be discovered at the worst moment.
                // bash -n is always available; shellcheck is used if installed.
                sh '''
                    set -eu
                    for f in $(find scripts infra -name '*.sh'); do
                        bash -n "$f"
                    done
                    echo "all shell scripts parse cleanly"
                    if command -v shellcheck >/dev/null 2>&1; then
                        shellcheck -S warning scripts/*.sh scripts/db/*.sh || true
                    fi
                '''
            }
        }

        stage('Build image') {
            steps {
                sh '''
                    set -eux
                    docker build \
                        --build-arg APP_VERSION="${GIT_SHA}" \
                        -t "${IMAGE_REF}" \
                        -t "${DOCKERHUB_USER}/${DOCKER_IMAGE}:latest" \
                        .
                    docker image inspect "${IMAGE_REF}" --format 'built {{.Id}} ({{.Size}} bytes)'
                '''
            }
        }

        stage('Push image') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'dockerhub-credentials',
                    usernameVariable: 'DOCKER_USER',
                    passwordVariable: 'DOCKER_PASS'
                )]) {
                    sh '''
                        set -eu
                        echo "${DOCKER_PASS}" | docker login -u "${DOCKER_USER}" --password-stdin
                        docker push "${IMAGE_REF}"
                        docker logout
                    '''
                }
                // The instance pulls by SHA, never by :latest, so a rollback
                // always names exactly one image.
                echo "pushed ${env.IMAGE_REF}"
            }
        }

        stage('Deploy') {
            steps {
                withCredentials([
                    sshUserPrivateKey(credentialsId: 'ec2-ssh-key', keyFileVariable: 'EC2_KEY_PATH'),
                    usernamePassword(credentialsId: 'aws-credentials',
                                     usernameVariable: 'AWS_ACCESS_KEY_ID',
                                     passwordVariable: 'AWS_SECRET_ACCESS_KEY')
                ]) {
                    script {
                        def drill = params.ROLLBACK_DRILL ? '--break-health' : ''
                        def status = sh(
                            returnStatus: true,
                            script: """
                                set -eu
                                "\${VENV}/bin/pip" install --quiet boto3
                                "\${VENV}/bin/python" deploy/deploy.py --tag "\${IMAGE_TAG}" ${drill}
                            """
                        )

                        if (status != 0) {
                            if (params.ROLLBACK_DRILL) {
                                // The drill is supposed to fail the health check.
                                // A green build here would mean rollback never ran.
                                currentBuild.result = 'UNSTABLE'
                                env.DEPLOY_OUTCOME = 'rolled-back-as-expected'
                                echo 'Rollback drill worked: the bad build was caught and torn down, previous version still live.'
                            } else {
                                env.DEPLOY_OUTCOME = 'rolled-back'
                                error("Deploy failed its health check and was rolled back automatically. The previous version is still serving traffic.")
                            }
                        } else {
                            env.DEPLOY_OUTCOME = 'deployed'
                        }
                    }
                }
            }
        }

        stage('Smoke test') {
            when { expression { env.DEPLOY_OUTCOME == 'deployed' } }
            steps {
                withCredentials([
                    sshUserPrivateKey(credentialsId: 'ec2-ssh-key', keyFileVariable: 'EC2_KEY_PATH'),
                    usernamePassword(credentialsId: 'aws-credentials',
                                     usernameVariable: 'AWS_ACCESS_KEY_ID',
                                     passwordVariable: 'AWS_SECRET_ACCESS_KEY')
                ]) {
                    script {
                        def status = sh(
                            returnStatus: true,
                            script: '"${VENV}/bin/python" deploy/healthcheck.py --expect-version "${IMAGE_TAG}"'
                        )
                        if (status != 0) {
                            // The build started and passed its own health check but
                            // does not actually work through nginx. This is the case
                            // deploy.py cannot catch, so roll back explicitly.
                            echo 'Smoke test failed against the public endpoint - rolling back to the previous known-good version.'
                            sh '"${VENV}/bin/python" deploy/rollback.py'
                            error('Post-deploy smoke test failed; rolled back to the previous version.')
                        }
                    }
                }
            }
        }
    }

    post {
        success {
            echo "SUCCESS: ${env.IMAGE_REF} is live."
        }
        unstable {
            echo "UNSTABLE: ${env.DEPLOY_OUTCOME}. This is the expected outcome of a rollback drill."
        }
        failure {
            echo "FAILURE: ${env.DEPLOY_OUTCOME ?: 'build failed before deploy'}. Live traffic is unaffected."
        }
        always {
            sh 'docker image prune -f --filter "until=72h" || true'
            cleanWs(deleteDirs: true, notFailBuild: true)
        }
    }
}
