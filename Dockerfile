FROM ubuntu:24.04 AS build

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       build-essential ca-certificates cmake git ninja-build python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
COPY . .
ARG BUILD_TYPE=Debug
ARG SANITIZER=
RUN cmake -S . -B build -G Ninja \
      -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
      -DSURGE_SANITIZER=${SANITIZER} \
    && cmake --build build --parallel

FROM ubuntu:24.04 AS runtime
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /work/build/surge /usr/local/bin/surge
ENTRYPOINT ["/usr/local/bin/surge"]
