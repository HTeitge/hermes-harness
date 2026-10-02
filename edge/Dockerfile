FROM alpine:3.22
RUN apk add --no-cache tinyproxy socat && mkdir -p /etc/tinyproxy /run/tinyproxy
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY --chmod=0755 entrypoint.sh /entrypoint.sh
EXPOSE 8888 9119 8642
ENTRYPOINT ["/entrypoint.sh"]
