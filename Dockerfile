# syntax=docker/dockerfile:1
# jev-nav live dashboard + ORB-SLAM3 slam_server in one image.
#   docker build -t jev-nav .                      (BUILD_JOBS=2 by default; each compiler needs ~2+ GB RAM)
#   docker build --build-arg BUILD_JOBS=1 ...      on small machines

############################ stage 1: ORB-SLAM3 + Pangolin ############################
FROM ubuntu:22.04 AS slam-build
ARG DEBIAN_FRONTEND=noninteractive
ARG BUILD_JOBS=2
# Portable CPU target (the VM that runs the image may differ from the build host). Use "native" only if you build where you run.
ARG MARCH=x86-64-v2
ARG PANGOLIN_REF=v0.6
ARG ORBSLAM3_COMMIT=4452a3c

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates pkg-config libeigen3-dev libopencv-dev \
        libboost-serialization-dev libssl-dev libglew-dev libepoxy-dev libgl1-mesa-dev \
        libwayland-dev libxkbcommon-dev wayland-protocols \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
# ORB-SLAM3 is built in /opt/ORB_SLAM3 because CMake bakes absolute build-tree RPATHs into the binaries.
RUN git clone --depth 1 --branch "${PANGOLIN_REF}" https://github.com/stevenlovegrove/Pangolin.git \
    && cmake -S Pangolin -B Pangolin/build -DCMAKE_BUILD_TYPE=Release -DBUILD_EXAMPLES=OFF -DBUILD_TOOLS=OFF \
    && cmake --build Pangolin/build -j"${BUILD_JOBS}" \
    && cmake --install Pangolin/build \
    && ldconfig \
    && rm -rf Pangolin

WORKDIR /opt
RUN git clone https://github.com/UZ-SLAMLab/ORB_SLAM3.git \
    && git -C ORB_SLAM3 checkout "${ORBSLAM3_COMMIT}"
COPY slam/orbslam3.patch /tmp/orbslam3.patch
COPY slam/slam_server.cc /opt/ORB_SLAM3/Examples/Monocular/slam_server.cc
WORKDIR /opt/ORB_SLAM3
RUN git apply --verbose /tmp/orbslam3.patch \
    && grep -rl --include=CMakeLists.txt -e '-march=native' . | xargs sed -i "s/-march=native/-march=${MARCH}/" \
    && for d in Thirdparty/DBoW2 Thirdparty/g2o; do \
         cmake -S "$d" -B "$d/build" -DCMAKE_BUILD_TYPE=Release \
         && cmake --build "$d/build" -j"${BUILD_JOBS}" || exit 1; \
       done \
    && (cd Vocabulary && tar -xzf ORBvoc.txt.tar.gz && rm ORBvoc.txt.tar.gz) \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --target slam_server -j"${BUILD_JOBS}"

# Stage the runtime tree with the original relative layout (the binaries use rpaths into lib/ and Thirdparty/*/lib).
RUN mkdir -p /stage/Examples/Monocular /stage/Vocabulary /stage/lib /stage/Thirdparty/DBoW2/lib /stage/Thirdparty/g2o/lib \
    && cp Examples/Monocular/slam_server /stage/Examples/Monocular/ \
    && cp lib/libORB_SLAM3.so /stage/lib/ \
    && cp Thirdparty/DBoW2/lib/libDBoW2.so /stage/Thirdparty/DBoW2/lib/ \
    && cp Thirdparty/g2o/lib/libg2o.so /stage/Thirdparty/g2o/lib/ \
    && cp Vocabulary/ORBvoc.txt /stage/Vocabulary/ \
    && mkdir -p /stage-pangolin \
    && find /usr/local -name 'libpangolin*.so*' -exec cp -a {} /stage-pangolin/ \; \
    && ls /stage-pangolin/libpangolin.so* \
    && (strip --strip-unneeded /stage/Examples/Monocular/slam_server /stage/lib/*.so /stage/Thirdparty/*/lib/*.so || true) \
    && ldd /stage/Examples/Monocular/slam_server

############################ stage 2: runtime ############################
FROM ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Runtime libs for slam_server (OpenCV 4.5 modules, GL stack, boost) and for the app (ffmpeg, portaudio, libGL for cv2).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl ffmpeg libportaudio2 libgl1 libglib2.0-0 libgomp1 \
        libopencv-core4.5d libopencv-imgproc4.5d libopencv-imgcodecs4.5d libopencv-features2d4.5d \
        libopencv-calib3d4.5d libopencv-flann4.5d libopencv-highgui4.5d libopencv-videoio4.5d \
        libboost-serialization1.74.0 libglew2.2 libepoxy0 libwayland-client0 libwayland-egl1 libxkbcommon0 \
        libegl1 libssl3 libopengl0 libglx0 libpng16-16 libjpeg-turbo8 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=slam-build /stage/ /opt/ORB_SLAM3/
# Pangolin v0.6 builds a shared libpangolin.so (installed in /usr/local/lib in the build stage); ship it and refresh the linker cache.
COPY --from=slam-build /stage-pangolin/ /usr/local/lib/
RUN ldconfig \
    && ldd /opt/ORB_SLAM3/Examples/Monocular/slam_server \
    && ! ldd /opt/ORB_SLAM3/Examples/Monocular/slam_server | grep "not found"

# Python 3.11 via uv (standalone build), venv in /opt/venv
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0
RUN uv venv --python 3.11 /opt/venv
ENV PATH=/opt/venv/bin:$PATH VIRTUAL_ENV=/opt/venv

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --python /opt/venv/bin/python ".[stream]" && rm -rf /build

# App
COPY streaming_server /app/streaming_server
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod 0755 /app/docker-entrypoint.sh \
    && useradd --system --create-home --uid 10001 jev \
    && mkdir -p /data/certs /data/captures && chown -R jev:jev /data

ENV HTTPS_PORT=8443 HTTP_PORT=8080 SLAM_PORT=5555 PUBLIC_HOST=localhost \
    SLAM_HOST=127.0.0.1 SLAM_AUTOSTART=0 \
    CERTS_DIR=/data/certs CAPTURES_DIR=/data/captures
VOLUME /data
WORKDIR /app
USER jev
# 8443 = HTTPS (phone), 8080 = local HTTP. The SLAM port (5555) is loopback-only and deliberately not exposed.
EXPOSE 8443 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${HTTP_PORT}/healthz" || exit 1
ENTRYPOINT ["/app/docker-entrypoint.sh"]
