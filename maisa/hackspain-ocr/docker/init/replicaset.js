/*
 * Inicia el replica set de un solo nodo y espera a que exista un PRIMARY.
 *
 * POR QUE NO VIVE EN /docker-entrypoint-initdb.d
 *
 * El entrypoint oficial de la imagen mongo arranca un mongod TEMPORAL sin
 * --replSet (y sin --auth) para poder ejecutar los scripts de inicializacion
 * como usuario sin credenciales. Cualquier rs.initiate() lanzado desde alli
 * falla con:
 *
 *   MongoServerError: This node was not started with replication enabled.
 *
 * Por eso este script se ejecuta desde un servicio aparte, una vez que el
 * contenedor de MongoDB esta sano y corriendo ya CON --replSet rs0.
 *
 * El script es idempotente: si el replica set ya esta iniciado no hace nada.
 */

const RS_NAME = process.env.MONGO_REPLSET_NAME || "rs0";
const MEMBER_HOST = process.env.MONGO_RS_HOST || "127.0.0.1:27017";
const MAX_INTENTOS = Number(process.env.MONGO_RS_MAX_ATTEMPTS || 90);

/*
 * Fallos PERMANENTES: reintentar no los arregla y ademas tapan el diagnostico
 * real detras del "no alcanzo el estado PRIMARY" del final.
 */
const ERRORES_PERMANENTES = new Map([
	[13, "Unauthorized: el keyFile no coincide o las credenciales no valen"],
	[23, "AlreadyInitialized: el replica set ya existe con OTRA configuracion"],
	[93, "AlreadyInitialized: el replica set ya existe con OTRA configuracion"],
	[94, "NotYetInitialized con una configuracion incompatible"],
]);

let ultimoMotivo = null;

function motivoDe(e) {
	if (!e) {
		return "error desconocido";
	}
	const codigo = e.code === undefined ? "" : "codigo " + e.code + ": ";
	return codigo + (e.codeName || e.message || String(e));
}

function hayPrimary() {
	try {
		const status = rs.status();
		return status.ok === 1 && status.members.some((m) => m.stateStr === "PRIMARY");
	} catch (e) {
		return false;
	}
}

function intentarIniciar() {
	// Si rs.status() responde, el replica set ya fue iniciado en un intento previo.
	try {
		rs.status();
		return;
	} catch (e) {
		// Todavia no esta iniciado: seguimos.
	}

	try {
		const cfg = { _id: RS_NAME, members: [{ _id: 0, host: MEMBER_HOST }] };
		print("[mongo-init] rs.initiate(" + JSON.stringify(cfg) + ")");
		rs.initiate(cfg);
	} catch (e) {
		ultimoMotivo = motivoDe(e);
		if (ERRORES_PERMANENTES.has(e && e.code)) {
			print("[mongo-init] ERROR PERMANENTE al iniciar el replica set -> " + ultimoMotivo);
			print("[mongo-init] " + ERRORES_PERMANENTES.get(e.code));
			quit(1);
		}
		// Esperado mientras el contenedor aun esta en la fase de initdb.
		print("[mongo-init] todavia no se puede iniciar (" + ultimoMotivo + "), reintentando...");
	}
}

for (let intento = 1; intento <= MAX_INTENTOS; intento++) {
	if (hayPrimary()) {
		print("[mongo-init] Replica set '" + RS_NAME + "' operativo (PRIMARY).");
		quit(0);
	}
	intentarIniciar();
	sleep(1000);
}

print("[mongo-init] ERROR: el replica set '" + RS_NAME + "' no alcanzo el estado PRIMARY"
	+ " tras " + MAX_INTENTOS + " intentos.");
if (ultimoMotivo !== null) {
	print("[mongo-init] ultimo error: " + ultimoMotivo);
}
quit(1);
