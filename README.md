# Sync de guías: Google Drive → GitHub → Mintlify

Las guías se escriben en Google Docs. Una GitHub Action las baja todos los días (o cuando le das al botón), las convierte a páginas de Mintlify y hace commit. Mintlify publica solo con cada push.

**Regla de oro: las guías se editan en Drive.** Las carpetas `guias/` e `images/guias/` se borran y se regeneran en cada sync; lo que se edite ahí (o en el editor web de Mintlify) se pierde.

## Archivos

```
.github/workflows/sync-guias.yml   # la Action (diaria + botón manual)
scripts/sync_drive.py              # baja de Drive, convierte y actualiza docs.json
scripts/requirements.txt
scripts/setup_gcp.sh               # configura Google Cloud (se corre una vez en Cloud Shell)
```

## Cómo se autentica con Google

**Sin llaves.** GitHub se identifica ante Google (Workload Identity Federation) y recibe un token de 1 hora con permiso solo de lectura en Drive. No hay ningún secreto guardado que se pueda filtrar, y funciona aunque la organización bloquee la creación de llaves de service account (`iam.disableServiceAccountKeyCreation`).

## Setup (una sola vez)

### 1. Carpeta en Drive
- Crea una carpeta, por ejemplo **Guías publicadas**, y deja ahí **un Google Doc por guía** (solo la versión final, sin copias).
- **Orden en el menú** = orden alfabético del nombre del archivo. Usa prefijos: `01 Montar un curso`, `02 Reportes`…
- **URL** = nombre del archivo sin el prefijo: `01 Montar un curso` → `/guias/montar-un-curso`. Si renombras el archivo, cambia la URL.
- **Título de la página** = la primera línea del Doc.
- Copia el ID de la carpeta: es lo que va después de `/folders/` en la URL.

### 2. Repo de Mintlify
1. Crea el proyecto en Mintlify; te deja un repo en GitHub.
2. Copia en ese repo los archivos de arriba, respetando las rutas.
3. En `docs.json` no tienes que hacer nada: si no existe un grupo `Guías` en la navegación, el script lo crea. Si prefieres ubicarlo tú (dentro de un tab, por ejemplo), crea `{"group": "Guías", "pages": []}` donde quieras y el script lo llena.
4. Anota el nombre completo del repo tal como sale en la URL de GitHub: `usuario-u-org/nombre-del-repo`.

### 3. Google Cloud (gratis)
1. Entra a [console.cloud.google.com](https://console.cloud.google.com) y crea un proyecto (ej. `lateacher-guias`). Anota su **ID**.
2. Abre **Cloud Shell** (botón `>_` arriba a la derecha).
3. Sube `scripts/setup_gcp.sh` (menú ⋮ → *Subir*) y córrelo:
   ```bash
   bash setup_gcp.sh ID_DEL_PROYECTO usuario-u-org/nombre-del-repo
   ```
4. Al final imprime dos cosas: el **correo de la service account** y los **valores para GitHub**.
5. **Comparte la carpeta de Drive con ese correo como Lector.**

### 4. GitHub
En el repo: **Settings → Secrets and variables → Actions → pestaña Variables**, crea:

| Variable | Valor |
| --- | --- |
| `GCP_WIF_PROVIDER` | el que imprimió el script (`projects/…/providers/repo-guias`) |
| `GCP_SERVICE_ACCOUNT` | `sync-guias@ID_DEL_PROYECTO.iam.gserviceaccount.com` |
| `DRIVE_FOLDER_ID` | el ID de la carpeta del paso 1 |

### 5. Primera corrida
**Actions → Sync guías desde Drive → Run workflow.** Si sale verde, en uno o dos minutos Mintlify publica.

## Día a día
- **Editar una guía**: edita el Doc. Se publica en el sync de la mañana, o al instante con *Run workflow*.
- **Nueva guía**: crea el Doc en la carpeta.
- **Quitar una guía**: sácala de la carpeta.

## Probar la conversión en local (opcional)
Descarga el Doc en markdown (*Archivo → Descargar → Markdown*) y:
```bash
pip install -r scripts/requirements.txt
python scripts/sync_drive.py --local carpeta_con_los_md
```

## Si algo falla
- **"No encontré Google Docs en la carpeta"**: la carpeta no está compartida con el correo de la service account, o el ID está mal. El script falla a propósito para no borrar las guías publicadas.
- **Falla el paso "Autenticar con Google"**: revisa que `GCP_WIF_PROVIDER` y `GCP_SERVICE_ACCOUNT` estén bien copiados y que el repo que le pasaste a `setup_gcp.sh` sea exactamente el mismo (mayúsculas incluidas). Puedes volver a correr el script sin problema.
- **El push falla con 403**: en *Settings → Actions → General → Workflow permissions* marca *Read and write permissions*.
- **No deja compartir la carpeta con el correo de la service account**: el Drive es de Workspace y bloquea compartir fuera del dominio. Pídele al admin que lo permita para ese correo, o usa una carpeta de un Drive que sí deje.
- **Repo público sin actividad por 60 días**: GitHub pausa los workflows programados. Se reactiva desde la pestaña Actions o con cualquier commit.
- **Una página no compila en Mintlify**: corre el script en local con `--local` sobre el `.md` de esa guía y revisa el `.mdx` que genera.

### Alternativa con llave JSON
Si algún día prefieres llave (por ejemplo, con un proyecto de Google Cloud creado desde una cuenta personal de Gmail, que no tiene esa política): crea la llave de la service account, guárdala completa como **secret** `GOOGLE_SERVICE_ACCOUNT_JSON` y borra la variable `GCP_WIF_PROVIDER`. El workflow detecta solo cuál usar.

## Qué arregla el script del export de Google
- Imágenes embebidas en base64 → archivos en `images/guias/<guia>/`.
- Secciones con H1 → bajan un nivel (Mintlify usa el título como H1).
- Tablas exportadas con encabezado vacío → la primera fila pasa a encabezado.
- Caracteres que rompen MDX (`{`, `}`, `<` sueltos y links `<https://...>`) → escapados.
