#!/bin/bash
# Path en el servidor remoto: /home/gonzalo/scripts/secure_gatekeeper.sh

CMD=$SSH_ORIGINAL_COMMAND
LOG_FILE="/home/gonzalo/scripts/secure_gatekeeper_log.txt"

# ── 1. Menú de Ayuda y Caso de Comando Vacío ──────────────────────────────────
if [ "$CMD" = "help" ] || [ "$CMD" = "--help" ] || [ -z "$CMD" ]; then
    cat << 'EOF'
🔒 SECURE GATEKEEPER - Comandos disponibles:

📦 DEPLOY:
  deploy pedidos_multi     - Despliega Pedidos Multi
  deploy meli_bunker       - Despliega Meli Bunker
  deploy agente_oriental   - Despliega Agente Oriental
  deploy agente_hibrido    - Despliega Agente Híbrido
  deploy bot_discord       - Despliega Bot Discord
  deploy api_seguros       - Despliega API Seguros
  deploy api_statica       - Despliega API Estática
  deploy blog_profesional  - Despliega Blog Profesional

🔍 AUDITORÍA:
  docker ps --format json
  systemctl status <servicio>
  tail -n 100 /var/log/syslog
  # ... y todos los comandos de la lista blanca del MCP

📊 ESTADO:
  help                     - Esta ayuda
EOF
    exit 0
fi

# Registrar el comando solicitado en el log
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Command requested: $CMD" >> "$LOG_FILE"

# ── 2. Lista Blanca de Despliegues ────────────────────────────────────────────
case "$CMD" in
    "deploy pedidos_multi")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/Pedidos_Multi/aplicacion_pedidos_multitenant"
        COMPOSE_FILE="docker-compose.prod.yml"
        ;;
    "deploy meli_bunker")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/Meli_Bunker/Meli-Bunker"
        COMPOSE_FILE="docker-compose.yml"
        ;;
    "deploy agente_oriental")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/Agente_Oriental"
        COMPOSE_FILE="docker-compose.yml"
        ;;
    "deploy agente_hibrido")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/agente_hibrido"
        COMPOSE_FILE="docker-compose.yml"
        ;;
    "deploy bot_discord")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/bot_discord"
        COMPOSE_FILE="docker-compose.yml"
        ;;
    "deploy api_seguros"|"deploy api_statica")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/api_statica"
        COMPOSE_FILE="docker-compose.yml"
        ;;
    "deploy blog_profesional"|"deploy pagina_blog_ponce")
        REPO_DIR="/home/gonzalo/Gonzalo_codigo/Blog_Profesional"
        COMPOSE_FILE="docker-compose.yml"
        ;;
    *)
        REPO_DIR=""
        ;;
esac

# Defensa en profundidad: Verificar si hay intentos de path traversal en REPO_DIR
if [[ "$REPO_DIR" =~ \.\. ]]; then
    echo "Acceso denegado: Ruta inválida"
    exit 1
fi

if [ -n "$REPO_DIR" ]; then
    if [ -d "$REPO_DIR" ]; then
        cd "$REPO_DIR" || exit 1
        git pull origin main >> "$LOG_FILE" 2>&1
        docker compose -f "$COMPOSE_FILE" up --build -d >> "$LOG_FILE" 2>&1
        echo "Despliegue de '$(echo "$CMD" | cut -d' ' -f2)' completado con éxito."
        exit 0
    else
        exit 1
    fi
fi

# ── 3. Lista Blanca de Comandos de Auditoría (Lectura) ────────────────────────
RX_CONTAINER='^[a-zA-Z0-9][a-zA-Z0-9_.-]*$'
RX_SERVICE='^[a-zA-Z0-9_.-]+$'

# Comandos con coincidencia exacta
if [ "$CMD" = "docker ps --format json" ] || \
   [ "$CMD" = "docker ps -a --format json" ] || \
   [ "$CMD" = "ss -tulnp" ] || \
   [ "$CMD" = "tailscale status --json" ] || \
   [ "$CMD" = "top -bn1" ] || \
   [ "$CMD" = "free -m" ] || \
   [ "$CMD" = "df -h" ] || \
   [ "$CMD" = "df -T" ] || \
   [ "$CMD" = "vcgencmd measure_temp" ] || \
   [ "$CMD" = "systemctl list-units --type=service" ] || \
   [ "$CMD" = "hostname" ] || \
   [ "$CMD" = "uptime" ]; then
    eval "$CMD"
    exit $?
fi

# Comando especial para medir temperaturas de CPU/SoC
TZ_CMD='for z in /sys/class/thermal/thermal_zone*; do echo "$(cat $z/type 2>/dev/null||echo unknown)|$(cat $z/temp 2>/dev/null||echo 0)"; done'
if [ "$CMD" = "$TZ_CMD" ]; then
    eval "$CMD"
    exit $?
fi

# docker inspect
if [[ "$CMD" =~ ^docker\ inspect\ --format\ json\ ([a-zA-Z0-9_.-]+)$ ]]; then
    CONTAINER="${BASH_REMATCH[1]}"
    if [[ "$CONTAINER" =~ $RX_CONTAINER ]]; then
        docker inspect --format json "$CONTAINER"
        exit $?
    fi
fi

# docker stats
if [[ "$CMD" =~ ^docker\ stats\ --no-stream\ --format\ json\ ([a-zA-Z0-9_.-]+)$ ]]; then
    CONTAINER="${BASH_REMATCH[1]}"
    if [[ "$CONTAINER" =~ $RX_CONTAINER ]]; then
        docker stats --no-stream --format json "$CONTAINER"
        exit $?
    fi
fi

# docker port
if [[ "$CMD" =~ ^docker\ port\ ([a-zA-Z0-9_.-]+)$ ]]; then
    CONTAINER="${BASH_REMATCH[1]}"
    if [[ "$CONTAINER" =~ $RX_CONTAINER ]]; then
        docker port "$CONTAINER"
        exit $?
    fi
fi

# docker logs
if [[ "$CMD" =~ ^docker\ logs\ (.*)$ ]]; then
    ARGS="${BASH_REMATCH[1]}"
    # Rechaza separadores, redireccion, sustitucion, backslash y newlines.
    if [[ "$ARGS" == *$'\n'* ]] || [[ "$ARGS" =~ [\;\|\&\>\<\$\(\)\`\\] ]]; then exit 1; fi
    eval "docker logs $ARGS"
    exit $?
fi

# systemctl status
if [[ "$CMD" =~ ^systemctl\ status\ ([a-zA-Z0-9_.-]+)$ ]]; then
    SERVICE="${BASH_REMATCH[1]}"
    if [[ "$SERVICE" =~ $RX_SERVICE ]]; then
        systemctl status "$SERVICE"
        exit $?
    fi
fi

# journalctl
if [[ "$CMD" =~ ^journalctl\ (.*)$ ]]; then
    ARGS="${BASH_REMATCH[1]}"
    # Rechaza separadores, redireccion, sustitucion, backslash y newlines.
    if [[ "$ARGS" == *$'\n'* ]] || [[ "$ARGS" =~ [\;\|\&\>\<\$\(\)\`\\] ]]; then exit 1; fi
    eval "journalctl $ARGS"
    exit $?
fi

# Función de seguridad para validar que las rutas de archivos estén dentro de directorios permitidos
check_path() {
    local path="$1"
    if [[ "$path" =~ \.\. ]]; then return 1; fi
    if [[ "$path" =~ ^/var/log/ ]] || [[ "$path" =~ ^/home/ ]] || [[ "$path" =~ ^/opt/ ]]; then return 0; fi
    return 1
}

# stat
if [[ "$CMD" =~ ^stat\ -c\ %s\ (.*)$ ]]; then
    PATH_ARG="${BASH_REMATCH[1]}"
    if check_path "$PATH_ARG"; then
        stat -c %s "$PATH_ARG"
        exit $?
    fi
fi

# readlink
if [[ "$CMD" =~ ^readlink\ -f\ (.*)$ ]]; then
    PATH_ARG="${BASH_REMATCH[1]}"
    if check_path "$PATH_ARG"; then
        readlink -f "$PATH_ARG"
        exit $?
    fi
fi

# tail de archivos de log
if [[ "$CMD" =~ ^tail\ -n\ ([0-9]+)\ (.*)$ ]]; then
    LINES="${BASH_REMATCH[1]}"
    PATH_ARG="${BASH_REMATCH[2]}"
    if [ "$LINES" -le 500 ] && check_path "$PATH_ARG"; then
        tail -n "$LINES" "$PATH_ARG"
        exit $?
    fi
fi

# ── 4. Verificación de Workspaces para el MCP (Solo Lectura Segura) ───────────
RX_TEST='^test -d (/home/gonzalo/Gonzalo_codigo/([a-zA-Z0-9_-]+)(/[a-zA-Z0-9_-]+)*) && echo EXISTS \|\| echo MISSING$'
RX_FIND='^find "(/home/gonzalo/Gonzalo_codigo/([a-zA-Z0-9_-]+)(/[a-zA-Z0-9_-]+)*)" -maxdepth ([12]) \\\(\ (.*) \\\)$'
RX_FIND_ARGS='^-name "[a-zA-Z0-9_.-]+"( -o -name "[a-zA-Z0-9_.-]+")*$'

# test -d para validar existencia del directorio de workspace
if [[ "$CMD" =~ $RX_TEST ]]; then
    TARGET_PATH="${BASH_REMATCH[1]}"
    test -d "$TARGET_PATH" && echo EXISTS || echo MISSING
    exit $?
fi

# find seguro para detectar archivos de compose y git
if [[ "$CMD" =~ $RX_FIND ]]; then
    TARGET_PATH="${BASH_REMATCH[1]}"
    DEPTH="${BASH_REMATCH[4]}"
    FIND_ARGS="${BASH_REMATCH[5]}"
    
    if [[ "$FIND_ARGS" =~ $RX_FIND_ARGS ]]; then
        eval "find \"$TARGET_PATH\" -maxdepth $DEPTH \( $FIND_ARGS \)"
        exit $?
    fi
fi

# ── 5. Comandos de Docker Compose ─────────────────────────────────────────────
# Comandos de Docker Compose limitados a workspaces bajo /home/gonzalo/Gonzalo_codigo/
RX_COMPOSE='^cd /home/gonzalo/Gonzalo_codigo/([a-zA-Z0-9_-]+)(/[a-zA-Z0-9_-]+)* && (docker compose .*)$'
if [[ "$CMD" =~ $RX_COMPOSE ]]; then
    WS_PATH="/home/gonzalo/Gonzalo_codigo/${BASH_REMATCH[1]}${BASH_REMATCH[2]}"
    COMPOSE_CMD="${BASH_REMATCH[3]}"
    if [ "$COMPOSE_CMD" = "docker compose ps --format json" ] || \
       [ "$COMPOSE_CMD" = "docker compose config" ] || \
       [ "$COMPOSE_CMD" = "docker compose config --services" ]; then
        cd "$WS_PATH" && eval "$COMPOSE_CMD"
        exit $?
    elif [[ "$COMPOSE_CMD" =~ ^docker\ compose\ logs\ (.*)$ ]]; then
        LOG_ARGS="${BASH_REMATCH[1]}"
        # Rechaza separadores, redireccion, sustitucion, backslash y newlines.
        if [[ "$LOG_ARGS" == *$'\n'* ]] || [[ "$LOG_ARGS" =~ [\;\|\&\>\<\$\(\)\`\\] ]]; then exit 1; fi
        cd "$WS_PATH" && eval "docker compose logs $LOG_ARGS"
        exit $?
    fi
fi

# Rechazar cualquier otro comando que no esté en la lista blanca
echo "Acceso denegado: Comando no autorizado."
exit 1
