for i in $(seq 1 50); do grep -qE '^#[0-9]+ ERROR|writing image|ERROR:' /root/build.log && break; sleep 10; done
grep -E '%\]' /root/build.log | tail -2; tail -3 /root/build.log; free -m | sed -n 2p
