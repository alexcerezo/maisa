use axum::{
    extract::{Multipart, State},
    http::StatusCode,
    response::IntoResponse,
    routing::post,
    Json, Router,
};
use mongodb::{bson::doc, options::ClientOptions, Client, Collection};
use serde::{Deserialize, Serialize};
use std::net::SocketAddr;
use tracing_subscriber;

// --- MODELOS ---

#[derive(Debug, Serialize, Deserialize)]
pub struct InvoiceData {
    pub emisor: Option<String>,
    pub total: Option<f64>,
    pub raw_text: String,
}

// Estado compartido para inyectar MongoDB en las rutas de Axum
#[derive(Clone)]
struct AppState {
    db_collection: Collection<InvoiceData>,
}

// --- PUNTO DE ENTRADA ---

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Inicializar logs (imprescindible para debugear en el hackathon)
    tracing_subscriber::fmt::init();

    // 1. Conexión a MongoDB
    let mongo_uri = std::env::var("MONGO_URI").unwrap_or_else(|_| "mongodb://localhost:27017".into());
    let mut client_options = ClientOptions::parse(mongo_uri).await?;
    let client = Client::with_options(client_options)?;
    let db = client.database("hackspain_db");
    let collection = db.collection::<InvoiceData>("invoices");

    let state = AppState {
        db_collection: collection,
    };

    // 2. Definir Rutas (Axum)
    let app = Router::new()
        .route("/api/upload-invoice", post(handle_upload))
        .with_state(state);

    // 3. Levantar Servidor
    let addr = SocketAddr::from(([0, 0, 0, 0], 3000));
    tracing::info!("🚀 Servidor Rust escuchando en {}", addr);
    
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app).await?;

    Ok(())
}

// --- HANDLER (Controlador HTTP) ---

async fn handle_upload(
    State(state): State<AppState>,
    mut multipart: Multipart,
) -> Result<Json<InvoiceData>, (StatusCode, String)> {
    
    // 1. Extraer el archivo PDF/Imagen del form-data
    let mut file_bytes = Vec::new();
    while let Some(field) = multipart.next_field().await.unwrap() {
        if field.name() == Some("file") {
            file_bytes = field.bytes().await.unwrap().to_vec();
            break;
        }
    }

    if file_bytes.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "No se envió ningún archivo".into()));
    }

    // 2. Procesar con PaddleOCR (Intentar API principal, si falla usar Local)
    let invoice_data = match process_ocr(&file_bytes).await {
        Ok(data) => data,
        Err(e) => {
            tracing::error!("Fallo catastrófico en OCR: {}", e);
            return Err((StatusCode::INTERNAL_SERVER_ERROR, "Fallo en el OCR".into()));
        }
    };

    // 3. Guardar en MongoDB
    match state.db_collection.insert_one(&invoice_data, None).await {
        Ok(insert_result) => tracing::info!("✅ Factura guardada con ID: {:?}", insert_result.inserted_id),
        Err(e) => tracing::error!("❌ Error guardando en Mongo: {}", e),
    }

    // 4. Devolver la respuesta al Frontend
    Ok(Json(invoice_data))
}

// --- SERVICIO OCR (El patrón Fallback) ---

async fn process_ocr(image_bytes: &[u8]) -> Result<InvoiceData, String> {
    let client = reqwest::Client::new();
    
    // URL de tu API en la nube (la rápida)
    let primary_api = "http://api-nube.tuservidor.com/ocr";
    // URL de tu ordenador portátil/servidor local en el hackathon (la segura)
    let fallback_api = "http://localhost:5000/ocr";

    tracing::info!("Intentando API principal de PaddleOCR...");
    
    match send_to_paddle(&client, primary_api, image_bytes).await {
        Ok(data) => {
            tracing::info!("⚡ OCR Principal respondió correctamente.");
            Ok(data)
        },
        Err(e) => {
            tracing::warn!("⚠️ API Principal caída ({}). Cambiando a Fallback Local...", e);
            
            // Si la principal falla, intentamos la local
            match send_to_paddle(&client, fallback_api, image_bytes).await {
                Ok(data) => {
                    tracing::info!("🛡️ OCR Fallback respondió correctamente.");
                    Ok(data)
                },
                Err(e) => Err(format!("Ambas APIs fallaron. Último error: {}", e))
            }
        }
    }
}

// Función auxiliar para hacer la petición HTTP POST a Python/PaddleOCR
async fn send_to_paddle(client: &reqwest::Client, url: &str, bytes: &[u8]) -> Result<InvoiceData, String> {
    // Aquí asumes que tu API de Python acepta los bytes crudos y devuelve JSON
    let response = client.post(url)
        .body(bytes.to_vec()) // En producción, es mejor enviarlo como multipart/form-data
        .send()
        .await
        .map_err(|e| e.to_string())?;

    if response.status().is_success() {
        let data: InvoiceData = response.json().await.map_err(|e| e.to_string())?;
        Ok(data)
    } else {
        Err(format!("Status code: {}", response.status()))
    }
}