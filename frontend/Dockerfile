FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html /usr/share/nginx/html/index.html
COPY command-center/ /usr/share/nginx/html/command-center/

EXPOSE 80
