#!/bin/sh
set -o allexport
. ./mongo-rs.env
docker build . -t martel/mongo-replica-ctrl:${CONTROLLER_TAG}
