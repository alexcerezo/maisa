import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ETIQUETA_ESCALON, ICONO_ESCALON, ICONO_ESCALON_DESCONOCIDO } from "./src/theme.ts";
const ESCALONES = ["capa_texto","cache_ocr","vision_ocr","vision_nube","degradado"];
const mapa = ICONO_ESCALON;
const etiquetas = ETIQUETA_ESCALON;
function pinta(c) { try { return renderToStaticMarkup(createElement(c, { className: "size-3.5" })); } catch (e) { return "LANZA: " + e.message.slice(0, 70); } }
console.log("--- contrato de 5 valores ---");
for (const e of ESCALONES) { const i = mapa[e]; const t = etiquetas[e]; if (!i) { console.log("  SIN ICONO:", e); continue; } if (!t) { console.log("  SIN ETIQUETA:", e); continue; } const h = pinta(i); console.log("  " + e.padEnd(13) + " \"" + t + "\" -> " + (h.startsWith("LANZA") ? h : "pinta OK")); }
const CASOS = [...ESCALONES, "vision_ocr_v2", null, undefined];
console.log("--- ANTES: acceso directo al mapa, sin red ---");
for (const v of CASOS) { const h = pinta(mapa[v]); console.log("  escalon=" + String(v).padEnd(14) + " " + (h.startsWith("LANZA") ? h : "pinta OK")); }
console.log("--- DESPUES: con la red del componente ---");
for (const v of CASOS) { const h = pinta(mapa[v] ?? ICONO_ESCALON_DESCONOCIDO); console.log("  escalon=" + String(v).padEnd(14) + " " + (h.startsWith("LANZA") ? h : "pinta OK")); }
