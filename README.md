This is the swarm manager and API endpoint.

To set up the server run these commands: 
- ```curl -fsSL get.docker.com | sh```
- ```docker swarm init```

To set up the container:
- Clone git repo (or download and install onto server via alternative method e.g., USB stick)
- ```cd {web-host-directory}```
- ```nano docker-compose.yaml```
- Comment out image and uncomment build
- CTRL+O
- CTRL+X
- ```docker compose build```
- ```nano docker-compose.yaml```
- Comment out build and uncomment image
- CTRL+O
- CTRL+X
- ```docker stack deploy -c docker-compose.yaml controller_and_traefik```
