# Derived Hermes image: bakes layer L2 (system gitconfig + pre-push hook) and the verify script
# into root-owned, read-only locations the agent user (uid 10000) cannot modify.
ARG HERMES_TAG=0.21.5
FROM nousresearch/hermes-agent:${HERMES_TAG}
USER root
COPY --chmod=0755 guard/git-hooks/ /opt/guard/git-hooks/
COPY --chmod=0755 guard/verify.sh  /opt/guard/verify.sh
COPY --chmod=0755 guard/seed-config.py /opt/guard/seed-config.py
COPY --chmod=0644 guard/gitconfig  /etc/gitconfig
# No credential helpers, no ssh client config, nothing to push with.
RUN rm -f /etc/ssh/ssh_config.d/* 2>/dev/null; \
    chown -R root:root /opt/guard && chmod -R a-w /opt/guard /etc/gitconfig
# Stay root: the image's s6 init (stage2) must run as root to remap the uid, chown the volume,
# migrate config.yaml and drop privileges to `hermes` per service. Upstream does the same.
