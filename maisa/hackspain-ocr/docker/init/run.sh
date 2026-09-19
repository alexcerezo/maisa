#!/bin/sh
# ---------------------------------------------------------------------------
# Servicio de arranque de un solo uso ("mongo-init").
#
# Deja el contenedor de MongoDB completamente operativo:
#   1. Espera a que mongod acepte conexiones.
#   2. Ejecuta docker/init/replicaset.js (inicia 'rs0', idempotente).
#   3. Sale con codigo 0 cuando existe un PRIMARY.
#
# Asi "docker compose up -d" deja un replica set listo para usar, sin pasos
# manuales, y sin chocar con la limitacion del entrypoint oficial descrita en
# replicaset.js.
# ---------------------------------------------------------------------------
set -u

HOST="${MONGO_INIT_HOST:-mongo:27017}"
USER="${MONGO_ROOT_USER:?MONGO_ROOT_USER es obligatorio}"
PASS="${MONGO_ROOT_PASSWORD:?MONGO_ROOT_PASSWORD es obligatorio}"
AUTH_DB="${MONGO_AUTH_DB:-admin}"

# Permite sobreescribir la URI completa (util si la contrasena contiene
# caracteres que haya que escapar en una URL).
URI="${MONGO_URI:-mongodb://${USER}:${PASS}@${HOST}/?authSource=${AUTH_DB}&directConnection=true&serverSelectionTimeoutMS=3000}"

echo "[mongo-init] Esperando a que mongod responda en ${HOST}..."
intento=0
while [ "$intento" -lt 120 ]; do
	if mongosh --quiet --host "$HOST" --eval 'db.adminCommand({ping:1}).ok' >/dev/null 2>&1; then
		break
	fi
	intento=$((intento + 1))
	sleep 1
done

echo "[mongo-init] Iniciando replica set..."
intento=0
while [ "$intento" -lt 60 ]; do
	# Puede fallar mientras mongod sigue en la fase de initdb (sin replica set,
	# sin autorizacion). Reintentamos hasta que el arranque real este listo.
	if mongosh "$URI" --quiet --file /init/replicaset.js; then
		echo "[mongo-init] MongoDB listo."
		exit 0
	fi
	intento=$((intento + 1))
	sleep 2
done

echo "[mongo-init] ERROR: no se pudo iniciar el replica set." >&2
exit 1
