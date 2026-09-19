// 02-app-user.js
// Crea el usuario de aplicación con mínimo privilegio.
//
// El usuario de app SOLO puede leer y escribir en la base de datos
// "albertitos". No puede administrar el servidor, ni tocar otras bases de
// datos. Ver diseño_logico.md §13.4.
//
// Idempotente: si el usuario ya existe, actualiza su contraseña y roles.

(function () {
  const dbName = process.env.MONGO_DB || "albertitos";
  const user = process.env.MONGO_APP_USER || "albertitos_app";
  const pwd = process.env.MONGO_APP_PASSWORD || "cambiame_tambien";

  const target = db.getSiblingDB(dbName);

  const existing = target.getUser(user);
  if (existing) {
    target.updateUser(user, {
      pwd: pwd,
      roles: [{ role: "readWrite", db: dbName }]
    });
    print(`[02-app-user] Usuario '${user}' actualizado en '${dbName}'.`);
    return;
  }

  target.createUser({
    user: user,
    pwd: pwd,
    roles: [{ role: "readWrite", db: dbName }]
  });
  print(`[02-app-user] Usuario '${user}' creado en '${dbName}' (readWrite).`);
})();