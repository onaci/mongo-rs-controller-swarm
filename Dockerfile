FROM python:3-alpine

RUN apk update \
    && apk upgrade \
    && apk --no-cache add \
        tini

RUN pip install --upgrade pip && \
    pip install docker==7.1.0 && \
    pip install pymongo==4.15.4

WORKDIR /src
COPY src/replica_ctrl.py ./

ENV MONGO_PORT=27017 \
    PYTHONUNBUFFERED=1

ENTRYPOINT [ "tini", "--" ]
CMD [ "python", "/src/replica_ctrl.py" ]
