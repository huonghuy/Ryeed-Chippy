# The app is stdlib-only and the server never opens USB, so vendor/ (pySerial) stays out.
FROM python:3.12-alpine3.24
EXPOSE 8765/tcp

# tini is the only added package; the app runs as a non-root user.
RUN apk add --no-cache tini && adduser -D -H -u 1000 chippy

WORKDIR /opt/chippy

# Named files only, so a local data/ (database, password hash) can't reach the image.
COPY --chown=chippy:chippy app.py mission.py shared_feed.py web_server.py ./
COPY --chown=chippy:chippy static static

RUN mkdir data && chown chippy:chippy data
VOLUME ["/opt/chippy/data"]

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER chippy

# tini forwards SIGTERM; Python as PID 1 would ignore it and docker stop would hang.
# Extra arguments (compose `command:`) are appended, e.g. --public-url.
ENTRYPOINT ["/sbin/tini", "--", "python3", "app.py", "--host", "0.0.0.0", "--no-browser"]
