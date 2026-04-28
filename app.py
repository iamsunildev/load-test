import os
import subprocess
import signal
import csv
import io
from flask import Flask, render_template, request, jsonify, Response

app = Flask(__name__)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

running_process = None


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload_csv():
    file = request.files.get("csv_file")
    if not file or file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    content = file.read()

    # Handle Excel files (.xlsx) - convert to CSV
    if file.filename.endswith((".xlsx", ".xls")):
        try:
            import openpyxl

            wb = openpyxl.load_workbook(io.BytesIO(content))
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                return jsonify({"error": "Excel file is empty"}), 400

            output = io.StringIO()
            writer = csv.writer(output)
            for row in rows:
                writer.writerow(row)
            csv_text = output.getvalue()
        except ImportError:
            return (
                jsonify(
                    {
                        "error": "Install openpyxl to upload Excel files: pip3 install openpyxl"
                    }
                ),
                400,
            )
    else:
        csv_text = content.decode("utf-8")

    # Validate CSV has required columns
    reader = csv.DictReader(io.StringIO(csv_text))
    fields = reader.fieldnames or []
    missing = [col for col in ("email", "password") if col not in fields]
    if missing:
        return (
            jsonify(
                {
                    "error": f"CSV missing required columns: {', '.join(missing)}. Found: {', '.join(fields)}"
                }
            ),
            400,
        )

    rows = list(reader)
    save_path = os.path.join(UPLOAD_DIR, "users.csv")
    with open(save_path, "w", newline="") as f:
        f.write(csv_text)

    return jsonify({"message": f"Uploaded {len(rows)} users", "count": len(rows)})


@app.route("/start", methods=["POST"])
def start_test():
    global running_process
    if running_process and running_process.poll() is None:
        return jsonify({"error": "A test is already running"}), 409

    data = request.json or {}
    users = data.get("users", 10)
    spawn_rate = data.get("spawn_rate", 5)
    duration = data.get("duration", "5m")
    host = data.get("host", "https://lexs.trainocate.co.jp")

    csv_path = os.path.join(UPLOAD_DIR, "users.csv")
    if not os.path.exists(csv_path):
        return jsonify({"error": "No CSV uploaded yet"}), 400

    env = os.environ.copy()
    env["LOCUST_CSV_PATH"] = csv_path

    cmd = [
        "python3",
        "-m",
        "locust",
        "-f",
        "index.py",
        "--headless",
        "-u",
        str(users),
        "-r",
        str(spawn_rate),
        "-t",
        str(duration),
        "--host",
        host,
    ]

    running_process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=os.path.dirname(__file__),
        bufsize=1,
        text=True,
    )

    return jsonify({"message": "Test started"})


@app.route("/logs")
def stream_logs():
    def generate():
        global running_process
        if not running_process:
            yield "data: No test running\n\n"
            return
        try:
            for line in iter(running_process.stdout.readline, ""):
                yield f"data: {line.rstrip()}\n\n"
            running_process.stdout.close()
            running_process.wait()
            yield f"data: [TEST COMPLETE] Exit code: {running_process.returncode}\n\n"
        except GeneratorExit:
            pass

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/stop", methods=["POST"])
def stop_test():
    global running_process
    if not running_process or running_process.poll() is not None:
        return jsonify({"message": "No test running"})

    running_process.send_signal(signal.SIGINT)
    running_process.wait(timeout=10)
    return jsonify({"message": "Test stopped"})


@app.route("/status")
def status():
    if running_process and running_process.poll() is None:
        return jsonify({"running": True})
    return jsonify({"running": False})


if __name__ == "__main__":
    app.run(port=8080)
