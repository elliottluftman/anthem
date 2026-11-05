import socket, json, sys, time
port = int(sys.argv[1]) if len(sys.argv) > 1 else 9800
wait = float(sys.argv[2]) if len(sys.argv) > 2 else 6
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", port)); srv.listen(1); srv.settimeout(15)
print("waiting for script on", port); conn, _ = srv.accept(); conn.settimeout(1.0)
buf = b""; t0 = time.time(); sent = False
while time.time() - t0 < wait + 10:
    try: chunk = conn.recv(65536)
    except socket.timeout: chunk = b""
    if chunk == b"" and not sent: pass
    buf += chunk
    while b"\n" in buf:
        line, buf = buf.split(b"\n", 1)
        try: m = json.loads(line)
        except Exception: continue
        if m.get("type") == "script_hello":
            m["token"] = "<redacted>"; print("hello:", json.dumps(m)[:300])
            conn.sendall((json.dumps({"type": "reload_code", "v": 1}) + "\n").encode()); sent = True; t1 = time.time()
        elif m.get("type") == "log": print("log:", m.get("msg"))
        else: print("msg:", m.get("type"))
    if sent and time.time() - t1 > wait: break
conn.close(); srv.close(); print("done")
