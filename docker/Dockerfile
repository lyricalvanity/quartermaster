# Quartermaster UI container: nginx serves the static page and proxies /api to the host agent,
# injecting the bearer key from a read-only mounted file. No build step, no node, no egress but the agent.
FROM nginx:1.27-alpine
RUN rm /etc/nginx/conf.d/default.conf
COPY ui/ /usr/share/nginx/html/
COPY docker/nginx.conf.template /etc/nginx/templates/quartermaster.conf.template
COPY docker/10-agent-key.envsh /docker-entrypoint.d/10-agent-key.envsh
RUN chmod +x /docker-entrypoint.d/10-agent-key.envsh
EXPOSE 80
