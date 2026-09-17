# Smart Wizard Inspiration Automation Engine

A high-performance FastAPI service and automated workflow engine for transforming **Smart Wizard 2D floorplan JSONs** into **3D Inspiration Engine room layouts**. The system processes room geometries in-memory, evaluates style and area constraints, logs test results to MySQL, and supports direct JSON uploads as well as Azure Blob Storage integration.

---

## Key Features

- **100% In-Memory Processing**: Zero disk file I/O operations for real-time payload generation and evaluation.
- **FastAPI Endpoints**: RESTful API endpoints for single-room testing and full floorplan multi-room automation.
- **Smart Wizard Floorplan Parser**: Extracts room boundaries, nominal room types, wall segments, door/window openings, and structural features.
- **Inspiration Engine Integration**: Maps room dimensions to style rules, area constraints, and item placements.
- **Azure Blob Storage Integration**: Fetch floorplan JSON files directly from Azure Blob containers.
- **MySQL & SQLite Logging**: Log execution latency, room status, error details, and `wizard_name` (`template_name`) per run.
- **Streamlit & Visualizer Tooling**: Interactive dashboards and HTML floorplan visualization utilities.

---

## Project Structure

```
SmartWizardInspiration/
├── server.py                            # FastAPI application & REST endpoint routes
├── create_room_inspiration_payload.py   # Core logic for converting Smart Wizard JSON to 3D room payloads
├── test_inspiration_api.py              # Inspiration Engine API automation client & test runner
├── azure_blob_service.py                # Azure Blob Storage manager
├── inspiration_db.py                    # MySQL & SQLite logging module
├── streamlit_app.py                     # Interactive Streamlit dashboard
├── visualize_smart_wizard.py            # Floorplan visualization tool
├── preview_floorplan.html               # Interactive HTML floorplan previewer
├── requirements.txt                     # Python package dependencies
├── .gitignore                           # Git ignore rules
└── README.md                            # Project documentation
```

---

## Installation & Setup

### 1. Prerequisites
- Python 3.10+
- MySQL database (optional, for result logging)

### 2. Install Dependencies
Create a virtual environment and install the required Python packages:

```bash
python -m venv venv
# On Windows:
.\venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### 3. Environment Configuration
Create a `.env` file in the project root directory with your database and Azure Blob credentials:

```env
# Database Credentials
DB_HOST=localhost
DB_USER=root
DB_PASSWORD=your_password
DB_NAME=smart_wizard_inspiration

# Azure Blob Storage
AZURE_STORAGE_CONNECTION_STRING=your_azure_connection_string
AZURE_CONTAINER_NAME=floorplans
```

---

## Usage

### 1. Running the FastAPI Server

Start the FastAPI development server with Uvicorn:

```bash
python server.py
# or
uvicorn server:app --reload --host 0.0.0.0 --port 8000
```

Access the interactive API documentation (Swagger UI) at:
`http://localhost:8000/docs`

---

## API Endpoints

### `POST /api/run-inspiration`

Executes 3D layout generation for a Smart Wizard floorplan.

#### Form Parameters / Multipart Upload:
- `wizard_name` *(string, optional)*: Custom name for the floorplan template (e.g. `Living Room` or `2BHK Unit A`).
- `file` *(file, optional)*: Smart Wizard floorplan JSON file upload.

#### Request Body (JSON):
```json
{
  "wizard_name": "2BHK Unit A",
  "floorplan": {
    "unit": "cm",
    "direction": "N",
    "layers": { ... }
  }
}
```

#### Example Response:
```json
{
  "status": "SUCCESS",
  "message": "Floorplan processing completed successfully. All rooms evaluated.",
  "success": true,
  "total_tested": 2,
  "passed": 2,
  "failed": 0,
  "rooms": [
    {
      "room_name": "Living Room",
      "status": "SUCCESS",
      "message": "3D layout generated successfully."
    },
    {
      "room_name": "Master Bedroom",
      "status": "SUCCESS",
      "message": "3D layout generated successfully."
    }
  ]
}
```

---

### `POST /api/automate-bhk`

Automates batch evaluation of room layouts across multi-BHK configurations.

---

## Visualization Tools

### 1. Streamlit Dashboard
Launch the interactive dashboard to inspect floorplan metrics and execution logs:

```bash
streamlit run streamlit_app.py
```

### 2. Smart Wizard Floorplan Visualizer
Visualize floorplan geometries and room layouts:

```bash
python visualize_smart_wizard.py
```

---

## License

Internal proprietary software for Smart Wizard Inspiration Automation.
