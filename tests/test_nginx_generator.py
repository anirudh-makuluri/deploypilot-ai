from graph.nodes.nginx_generator import nginx_generator_node


def test_nginx_generator_strips_ws_location_when_not_indicated():
    state = {
        "services": [{"name": "web", "port": 3000}],
        "repo_scan": {
            "key_files": {
                "nginx.conf": """
events { worker_connections 1024; }
http {
  server {
    listen 80;
    location / {
      proxy_pass http://localhost:3000;
    }
    location /ws {
      proxy_pass http://localhost:4001;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
    }
  }
}
""",
                "package.json": '{"name":"frontend-app"}',
            }
        },
    }

    out = nginx_generator_node(state)

    assert "location /ws" not in out["nginx_conf"].lower()


def test_nginx_generator_keeps_ws_location_when_ws_evidence_exists():
    state = {
        "services": [
            {"name": "web", "port": 3000},
            {"name": "api", "port": 5000},
        ],
        "repo_scan": {
            "key_files": {
                "nginx.conf": """
events { worker_connections 1024; }
http {
  server {
    listen 80;
    location / {
      proxy_pass http://localhost:3000;
    }
    location /ws {
      proxy_pass http://localhost:4001;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
    }
  }
}
""",
                "src/socket.ts": 'const socket = new WebSocket("ws://localhost:4001");',
            }
        },
    }

    out = nginx_generator_node(state)

    assert "location /ws" in out["nginx_conf"].lower()
