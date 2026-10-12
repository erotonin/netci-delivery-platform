# A stand-in for a production VM in the lab: systemd as PID 1, sshd, python3 for
# Ansible, and a `netci` user that may become root. Built with --network=host because
# containers on the lab network are deliberately offline (no NAT), which is also why
# the running host has no route to package mirrors -- exactly like an air-gapped prod.
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
# docker.io + the compose plugin: the host is also the docker runtime's production
# target (ADR-028 follow-up), so the worker's own daemon is no longer the only one the
# docker playbook has ever deployed to. The lab registry is plaintext; a real host
# would trust a CA instead of listing an insecure registry.
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      systemd systemd-sysv openssh-server python3 sudo curl ca-certificates \
      docker.io docker-compose-v2 iptables python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash netci \
    && echo "netci ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/netci \
    && mkdir -p /home/netci/.ssh && chmod 700 /home/netci/.ssh && chown -R netci:netci /home/netci/.ssh \
    && mkdir -p /etc/docker \
    && printf '{"insecure-registries": ["172.17.0.1:55000"], "features": {"containerd-snapshotter": false}}\n' > /etc/docker/daemon.json \
    && systemctl enable ssh docker \
    && rm -f /etc/ssh/ssh_host_*
# Host keys are generated on first boot so each container has its own.
RUN printf '[Unit]\nDescription=generate ssh host keys\nBefore=ssh.service\nConditionPathExists=!/etc/ssh/ssh_host_ed25519_key\n[Service]\nType=oneshot\nExecStart=/usr/bin/ssh-keygen -A\n[Install]\nWantedBy=multi-user.target\n' > /etc/systemd/system/ssh-hostkeys.service \
    && systemctl enable ssh-hostkeys
# The edge agent runs *here*, on the host it reports about (see netci-agent@.service).
# Its telemetry is what the DCIM pre-flight gate reads before a release lands on this
# host; an agent elsewhere reporting under this name would be reporting another
# machine's disk.
# Jammy's python3-websockets (9.1) no longer works with its own Python 3.10 ("the *loop*
# parameter was removed"); the wheel is installed at build time, when there is network.
RUN pip3 install --no-cache-dir "websockets==15.0.1"
COPY agent_daemon.py /opt/netci-agent/agent_daemon.py
COPY netci-agent@.service /etc/systemd/system/netci-agent@.service
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
