// TCP front-end for monocular ORB-SLAM3, used by jev-nav's live dashboard.
//
// Protocol (little-endian), one client at a time:
//   client -> server  header:  uint32 width, uint32 height          (size of every frame that follows)
//   server -> client  "READY\n" once the vocabulary is loaded and SLAM is running
//   client -> server  per frame: uint32 jpeg_len, double timestamp_s, jpeg bytes
//   client -> server  RESET: uint32 0xFFFFFFFF alone; server resets the map and replies with an empty state line
//   server -> client  per frame: one JSON line  {"state":int,"pose":[x,y,z,r00..r22],"pts":[[id,x,y,z,u,v],..]}  (u,v = pixel where it was seen)
// state: -1 no images yet, 0 not initialised, 1 initialising, 2 tracking, 3 lost.
// pose is the camera centre and camera-to-world rotation (row-major) in the map frame; pts are the map points
// tracked in this frame (world coordinates, arbitrary monocular scale).
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <sstream>
#include <vector>

#include <opencv2/core/core.hpp>
#include <opencv2/imgcodecs.hpp>

#include "System.h"

using namespace std;

static bool readAll(int fd, void *buf, size_t n) {
    char *p = static_cast<char *>(buf);
    while (n > 0) {
        ssize_t r = recv(fd, p, n, 0);
        if (r <= 0) return false;
        p += r;
        n -= static_cast<size_t>(r);
    }
    return true;
}

static bool writeAll(int fd, const string &s) {
    const char *p = s.data();
    size_t n = s.size();
    while (n > 0) {
        ssize_t r = send(fd, p, n, MSG_NOSIGNAL);
        if (r <= 0) return false;
        p += r;
        n -= static_cast<size_t>(r);
    }
    return true;
}

// Approximate pinhole intrinsics for an uncalibrated phone/webcam: fx = fy = 0.9 * the longer side, no distortion.
static string writeSettings(uint32_t w, uint32_t h) {
    const double f = 0.9 * max(w, h);
    const string path = "/tmp/slam_server_" + to_string(w) + "x" + to_string(h) + ".yaml";
    ofstream y(path);
    y << fixed << setprecision(3);
    y << "%YAML:1.0\nFile.version: \"1.0\"\nCamera.type: \"PinHole\"\n"
      << "Camera1.fx: " << f << "\nCamera1.fy: " << f << "\nCamera1.cx: " << w / 2.0 << "\nCamera1.cy: " << h / 2.0 << "\n"
      << "Camera1.k1: 0.0\nCamera1.k2: 0.0\nCamera1.p1: 0.0\nCamera1.p2: 0.0\n"
      << "Camera.width: " << w << "\nCamera.height: " << h << "\nCamera.fps: 15\nCamera.RGB: 0\n"
      << "ORBextractor.nFeatures: 1500\nORBextractor.scaleFactor: 1.2\nORBextractor.nLevels: 8\n"
      << "ORBextractor.iniThFAST: 20\nORBextractor.minThFAST: 7\n"
      << "Viewer.KeyFrameSize: 0.05\nViewer.KeyFrameLineWidth: 1.0\nViewer.GraphLineWidth: 0.9\nViewer.PointSize: 2.0\n"
      << "Viewer.CameraSize: 0.08\nViewer.CameraLineWidth: 3.0\nViewer.ViewpointX: 0.0\nViewer.ViewpointY: -0.7\n"
      << "Viewer.ViewpointZ: -1.8\nViewer.ViewpointF: 500.0\n";
    return path;
}

static void serve(int fd, const string &vocab) {
    uint32_t hdr[2];
    if (!readAll(fd, hdr, sizeof hdr)) return;
    const uint32_t w = hdr[0], h = hdr[1];
    if (w < 64 || h < 64 || w > 4096 || h > 4096) return;
    cerr << "[slam_server] client " << w << "x" << h << ", starting SLAM" << endl;

    ORB_SLAM3::System slam(vocab, writeSettings(w, h), ORB_SLAM3::System::MONOCULAR, false);
    if (!writeAll(fd, "READY\n")) { slam.Shutdown(); return; }

    vector<uchar> jpeg;
    while (true) {
        uint32_t len;
        double ts;
        if (!readAll(fd, &len, 4)) break;
        if (len == 0xFFFFFFFFu) {  // RESET: forget the map and start over (keeps the vocabulary loaded)
            slam.Reset();
            if (!writeAll(fd, "{\"state\":0,\"pose\":null,\"pts\":[]}\n")) break;
            continue;
        }
        if (!readAll(fd, &ts, 8) || len == 0 || len > (16u << 20)) break;
        jpeg.resize(len);
        if (!readAll(fd, jpeg.data(), len)) break;
        cv::Mat im = cv::imdecode(jpeg, cv::IMREAD_COLOR);
        if (im.empty() || (uint32_t)im.cols != w || (uint32_t)im.rows != h) {
            if (!writeAll(fd, "{\"state\":-1,\"pose\":null,\"pts\":[]}\n")) break;
            continue;
        }
        Sophus::SE3f Tcw = slam.TrackMonocular(im, ts);
        const int state = slam.GetTrackingState();

        ostringstream o;
        o << "{\"state\":" << state << ",\"pose\":";
        if (state == 2) {
            Sophus::SE3f Twc = Tcw.inverse();
            const Eigen::Vector3f t = Twc.translation();
            const Eigen::Matrix3f R = Twc.rotationMatrix();
            o << "[" << t.x() << "," << t.y() << "," << t.z();
            for (int r = 0; r < 3; r++)
                for (int c = 0; c < 3; c++) o << "," << R(r, c);
            o << "]";
        } else {
            o << "null";
        }
        o << ",\"pts\":[";
        bool first = true;
        if (state == 2) {
            // GetTrackedMapPoints() and GetTrackedKeyPointsUn() are index-aligned: keypoint i is where map point i was seen
            const vector<ORB_SLAM3::MapPoint *> mps = slam.GetTrackedMapPoints();
            const vector<cv::KeyPoint> kps = slam.GetTrackedKeyPointsUn();
            for (size_t i = 0; i < mps.size(); i++) {
                ORB_SLAM3::MapPoint *mp = mps[i];
                if (!mp || mp->isBad()) continue;
                const Eigen::Vector3f p = mp->GetWorldPos();
                o << (first ? "" : ",") << "[" << mp->mnId << "," << p.x() << "," << p.y() << "," << p.z();
                if (i < kps.size()) o << "," << kps[i].pt.x << "," << kps[i].pt.y;
                o << "]";
                first = false;
            }
        }
        o << "]}\n";
        if (!writeAll(fd, o.str())) break;
    }
    cerr << "[slam_server] client gone, shutting SLAM down" << endl;
    slam.Shutdown();
}

int main(int argc, char **argv) {
    if (argc < 2) {
        cerr << "Usage: slam_server path_to_vocabulary [port=5555] [bind_address=127.0.0.1]" << endl;
        return 1;
    }
    const int port = argc > 2 ? atoi(argv[2]) : 5555;
    int ls = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    // No authentication on this socket: loopback only unless the caller explicitly asks otherwise.
    const char *bindAddr = argc > 3 ? argv[3] : "127.0.0.1";
    if (inet_pton(AF_INET, bindAddr, &addr.sin_addr) != 1) {
        cerr << "[slam_server] invalid bind address: " << bindAddr << endl;
        return 1;
    }
    addr.sin_port = htons(port);
    if (bind(ls, reinterpret_cast<sockaddr *>(&addr), sizeof addr) != 0 || listen(ls, 1) != 0) {
        perror("bind/listen");
        return 1;
    }
    cerr << "[slam_server] listening on " << bindAddr << ":" << port << endl;
    while (true) {
        int fd = accept(ls, nullptr, nullptr);
        if (fd < 0) continue;
        setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
        serve(fd, argv[1]);
        close(fd);
    }
}
