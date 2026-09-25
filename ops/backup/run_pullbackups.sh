#!/bin/bash
exec docker run --rm --name botkit-pullbacks-runner -v /home/deploy:/home/deploy -v /var/run/docker.sock:/var/run/docker.sock -v /usr/bin/docker:/usr/bin/docker:ro debian:bookworm-slim bash /home/deploy/pullbackups.sh
