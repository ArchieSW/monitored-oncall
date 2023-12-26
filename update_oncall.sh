#!/bin/bash

while getopts v:c:n: flag
do
  case "${flag}" in
    v) version_to_update=${OPTARG};;
    c) container_to_update=${OPTARG};;
    n) nginx_conf=${OPTARG};;
  esac
done

if [ ! -f "$nginx_conf" ]
then
  echo "No such nginx.conf file found"
  exit 1
fi

echo "Building new image version from github: https://github.com/linkedin/oncall.git#$version_to_update"
built_new_version=$(docker build --tag oncall:$version_to_update -q https://github.com/linkedin/oncall.git#$version_to_update)
echo "New image hash: $built_new_version"

# TODO: should be replaced with nginx config reloading
echo "Turning server down in nginx.conf"
string_to_replace=$(grep -E -i "server $container_to_update:8080 .*;" $nginx_conf)
down_container_string=$(echo $string_to_replace | cut -d ';' -f 1 | sed 's/$/ down;/')
sed -i "s/$string_to_replace/        $down_container_string/g" $nginx_conf

echo "Restarting nginx"
docker compose restart nginx

echo "Stopping and deleting  container to update: $container_to_update"
docker stop $container_to_update
docker rm $container_to_update

sleep 2s
echo "Starting new image"
docker run -d \
           --name=$container_to_update \
           --hostname=oncall-inner \
           -e DOCKER_DB_BOOTSTRAP=1 -e IRIS_API_HOST=iris \
           -v ./configs/config.docker.yaml:/home/oncall/config/config.yaml \
           --restart unless-stopped \
           --network iris \
           oncall:$version_to_update

echo "Turning server up in nginx.conf"
sed -i "s/$down_container_string/$string_to_replace/g" $nginx_conf

echo "Restarting nginx"
docker compose restart nginx
