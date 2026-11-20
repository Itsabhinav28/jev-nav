# ORB-SLAM3 patches

The Docker image builds upstream ORB-SLAM3 (UZ-SLAMLab, commit 4452a3c) with these local changes:

- `orbslam3.patch`: builds with C++14 (needed by current Eigen/Sophus), removes `-Werror` from Sophus, adds the
  `slam_server` CMake target, and fixes a segfault in `ORBextractor.cc` (`nIni` was 0 for portrait frames).
- `slam_server.cc`: small TCP front-end used by `streaming_server/slam_bridge.py` (protocol documented in the file header).
  It has no authentication, so it binds to 127.0.0.1 by default; a third argument overrides the bind address.

The Dockerfile also swaps `-march=native` for a portable `-march=$MARCH` (default x86-64-v2).
