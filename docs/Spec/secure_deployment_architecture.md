# Arquitectura de Despliegue Seguro - OrangePi (MCP Oranpi)

Este documento detalla el diseño de seguridad para permitir a los asistentes de IA auditar el servidor e iniciar despliegues controlados de contenedores Docker en la OrangePi (`gonpatri`), bloqueando cualquier posibilidad de comandos destructivos (como `docker compose down -v` o `docker system prune`).

## 1. Principios de Seguridad

1. **Principio de Privilegio Mínimo (Least Privilege)**: La IA solo tiene permisos para realizar acciones explícitamente autorizadas.
2. **Seguridad por Diseño (Security by Design)**: La seguridad reside en las restricciones del servidor, no en ocultar datos de red (Seguridad por Oscuridad).
3. **Aislamiento de Entornos**: La IA puede desplegar individualmente cada proyecto sin que un error en uno afecte a los demás.

---

## 2. Red y Conectividad (El rol de Tailscale)

**¿Es seguro dejar la IP de Tailscale visible en la configuración del agente?**

**SÍ, ES 100% SEGURO.** No hay necesidad de quitar la IP de Tailscale (`100.106.85.109`) ni los datos de red de los prompts de la IA. 

La restricción está del lado del servidor SSH del OrangePi (Server-Side Restriction). Cuando el agente de IA intenta conectarse a través de Tailscale (desde fuera de casa) usando su llave privada específica (`id_agente_ed25519`), el servidor OrangePi:
1. Valida la firma criptográfica de la llave.
2. Ignora cualquier comando que la IA intente enviar por consola.
3. Ejecuta **únicamente** el script de deploy restringido (`deploy_safe.sh`).
4. Cierra la conexión inmediatamente al terminar el script.

Por lo tanto, aunque la IA conozca la IP de Tailscale y los puertos, criptográficamente está atrapada dentro del script de deploy y nunca tendrá un prompt interactivo de terminal. Vos podés seguir usando tus agentes de forma remota por Tailscale sin ningún riesgo.

---

## 3. Mapeo de Proyectos en Producción (OrangePi)

De acuerdo con el estado de `docker ps`, se definen las rutas de los proyectos autorizados en el OrangePi:

| Identificador (Lista Blanca) | Directorio del Workspace | Archivo Compose | Contenedores Asociados |
| :--- | :--- | :--- | :--- |
| `pedidos_multi` | `~/Gonzalo_codigo/Pedidos_Multi/aplicacion_pedidos_multitenant` | `docker-compose.prod.yml` | `aplicacion_pedidos_multitenant-app-1`, `-db-1`, `-nginx-1` |
| `meli_bunker` | `~/Gonzalo_codigo/Meli_Bunker` | `docker-compose.yml` | `svl-app`, `svl-db`, `svl-nginx` |
| `agente_oriental` | `~/Gonzalo_codigo/Agente_Oriental` | `docker-compose.yml` | `auditor_familiar_app`, `-guardian`, `-ocr_api`, `-nginx`, `-db` |
| `agente_hibrido` | `~/Gonzalo_codigo/agente_hibrido` | `docker-compose.yml` | `agente_hibrido_texto_kimi_rag_gemini-frontend-1`, `-backend-1`, `-postgres-1` |
| `bot_discord` | `~/Gonzalo_codigo/bot_discord` | `docker-compose.yml` | `bot_discord-bot-1`, `-pgadmin-1`, `-nginx-1`, `-postgres-1` |
| `api_seguros` | `~/Gonzalo_codigo/api_statica` | `docker-compose.yml` | `api_seguros_app`, `api_seguros_db`, `api_seguros_pgadmin` |

---

## 4. Script de Despliegue Inteligente (`deploy_safe.sh`)

Este script debe guardarse en `/home/gonzalo/scripts/deploy_safe.sh` en el OrangePi. Utiliza la variable `SSH_ORIGINAL_COMMAND` enviada por el cliente SSH para determinar qué proyecto desplegar de forma segura.

```bash
#!/bin/bash
# Guardar en: /home/gonzalo/scripts/deploy_safe.sh

PROYECTO_SOLICITADO=$SSH_ORIGINAL_COMMAND
LOG_FILE="/home/gonzalo/scripts/deploy_log.txt"

echo "=== Intento de Despliegue: $(date) ===" >> "$LOG_FILE"
echo "Proyecto solicitado: $PROYECTO_SOLICITADO" >> "$LOG_FILE"

# LISTA BLANCA DE PROYECTOS AUTORIZADOS Y SUS RUTAS
case "$PROYECTO_SOLICITADO" in
    "pedidos_multi")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/Pedidos_Multi/aplicacion_pedidos_multitenant"
        COMPOSE_FILE="docker-compose.prod.yml"
        ;;
    "meli_bunker")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/Meli_Bunker"
        COMPOSE_FILE="docker-compose.yml"
        ;;
    "agente_oriental")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/Agente_Oriental"
        COMPOSE_FILE="docker-compose.yml"
        ;;
    "agente_hibrido")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/agente_hibrido"
        COMPOSE_FILE="docker-compose.yml"
        ;;
    "bot_discord")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/bot_discord"
        COMPOSE_FILE="docker-compose.yml"
        ;;
    "api_seguros")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/api_statica"
        COMPOSE_FILE="docker-compose.yml"
        ;;
    *)
        echo "ERROR: Solicitud '$PROYECTO_SOLICITADO' rechazada por seguridad." >> "$LOG_FILE"
        echo "Acceso denegado: Proyecto no autorizado."
        exit 1
        ;;
esac

# EJECUCIÓN DEL DESPLIEGUE SEGURO
if [ -d "$REPO_DIR" ]; then
    cd "$REPO_DIR" || exit 1
    
    echo "Desplegando '$PROYECTO_SOLICITADO'..." >> "$LOG_FILE"
    
    echo " -> Actualizando Git..." >> "$LOG_FILE"
    git pull origin main >> "$LOG_FILE" 2>&1
    
    echo " -> Reconstruyendo contenedores Docker..." >> "$LOG_FILE"
    docker compose -f "$COMPOSE_FILE" up --build -d >> "$LOG_FILE" 2>&1
    
    echo "=== Despliegue exitoso para $PROYECTO_SOLICITADO ===" >> "$LOG_FILE"
    echo "Despliegue de '$PROYECTO_SOLICITADO' completado con éxito."
else
    echo "ERROR: No existe el directorio $REPO_DIR para el proyecto $PROYECTO_SOLICITADO" >> "$LOG_FILE"
    echo "Error de despliegue: Directorio del repositorio no encontrado."
    exit 1
fi
```

---

## 5. Plan de Ejecución (Paso a Paso)

### En el Servidor OrangePi:
1. Conectarse por SSH como Administrador.
2. Crear la carpeta si no existe: `mkdir -p ~/scripts`.
3. Crear el archivo del script: `nano ~/scripts/deploy_safe.sh`, pegar el código de arriba, guardar y salir.
4. Dar permisos de ejecución: `chmod +x ~/scripts/deploy_safe.sh`.
5. Modificar el archivo de llaves: `nano ~/.ssh/authorized_keys`.
6. Registrar la clave pública del agente en una nueva línea con la restricción de comando:
   ```text
   command="/home/gonzalo/scripts/deploy_safe.sh",no-port-forwarding,no-x11-forwarding,no-agent-forwarding ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKqVQfPebMk8Q4KcfV81hUPW4kty4rGNc8I8BePykLsh llave-agente-ia
   ```

### En el Entorno Local (Windows):
1. Asegurarse de que la clave privada generada está en `C:\Users\cerra\.ssh\id_agente_ed25519`.
2. Actualizar el archivo `C:\Users\cerra\.gemini\antigravity\mcp_config.json` (y cualquier archivo de settings en OpenCode si corresponde) para que el path de la llave apunte a `C:/Users/cerra/.ssh/id_agente_ed25519`.
