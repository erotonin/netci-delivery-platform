# A stand-in for a production VM in the lab: systemd as PID 1, sshd, python3 for
# Ansible, and a `netci` user that may become root. Built with --network=host because
# containers on the lab network are deliberately offline (no NAT), which is also why
# the running host has no route to package mirrors -- exactly like an air-gapped prod.
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      systemd systemd-sysv openssh-server python3 sudo curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash netci \
    && echo "netci ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/netci \
    && mkdir -p /home/netci/.ssh && chmod 700 /home/netci/.ssh && chown -R netci:netci /home/netci/.ssh \
    && systemctl enable ssh \
    && rm -f /etc/ssh/ssh_host_*
# Host keys are generated on first boot so each container has its own.
RUN printf '[Unit]\nDescription=generate ssh host keys\nBefore=ssh.service\nConditionPathExists=!/etc/ssh/ssh_host_ed25519_key\n[Service]\nType=oneshot\nExecStart=/usr/bin/ssh-keygen -A\n[Install]\nWantedBy=multi-user.target\n' > /etc/systemd/system/ssh-hostkeys.service \
    && systemctl enable ssh-hostkeys
STOPSIGNAL SIGRTMIN+3
CMD ["/lib/systemd/systemd"]
