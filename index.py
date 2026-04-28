from locust import HttpUser, task, between
from locust.exception import StopUser
import json
import logging
import time
import csv
from threading import Lock
from datetime import datetime
import sys
import os

for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

log_filename = f"lab_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_filename)],
)

logger = logging.getLogger("locust")

# ----------------- CSV Setup -----------------
CSV_PATH = os.environ.get("LOCUST_CSV_PATH", "users.csv")

with open(CSV_PATH) as f:
    reader = csv.DictReader(f)
    user_pool = list(reader)

user_index = 0
user_lock = Lock()


# ----------------- Locust User Class -----------------
class LoggedInUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        global user_index
        with user_lock:
            if user_index >= len(user_pool):
                logger.info("✅ All users have been used. Skipping this user.")
                raise StopUser()
            self.user = user_pool[user_index]
            user_index += 1

        logger.info(f"\n🔐 Starting session for user: {self.user['email']}")
        self.authenticate_user()

    def authenticate_user(self):
        # time.sleep(3)  # throttle before lab start to avoid DB contention
        login_url = "https://t3yd6syl4j.execute-api.ap-northeast-1.amazonaws.com/production/auth/login"
        login_payload = {
            "email": self.user["email"],
            "password": "Welcome@123",
            "voucher_code": None,
        }
        headers = {"Content-Type": "application/json"}

        try:
            res = self.client.post(
                login_url, data=json.dumps(login_payload), headers=headers, name="Login"
            )
            response_text = res.text
            try:
                response_json = res.json()
            except json.JSONDecodeError:
                logger.error(
                    f"❌ Login response not JSON for {self.user['email']}: {response_text}"
                )
                raise StopUser("Invalid JSON in login response")

            if res.status_code != 200 or not isinstance(response_json, dict):
                logger.error(
                    f"❌ Login failed for {self.user['email']}: {res.status_code} - {response_text}"
                )
                raise StopUser("Login API failed")

            user_token = response_json.get("data", {}).get("token")
            if not user_token:
                logger.error(
                    f"❌ Token missing in login response for {self.user['email']}"
                )
                raise StopUser("Missing token")
        except StopUser:
            raise
        except Exception as e:
            logger.error(f"❌ Login exception for {self.user['email']}: {e}")
            raise StopUser()

        # Whizlabs Auth
        auth_url = "https://t3yd6syl4j.execute-api.ap-northeast-1.amazonaws.com/production/users/user-authentication"
        auth_payload = {"user_token": user_token, "pt": 11}

        try:
            res = self.client.post(
                auth_url,
                data=json.dumps(auth_payload),
                headers=headers,
                name="User Authentication",
            )
            if res.status_code != 200:
                logger.error(
                    f"❌ Auth failed with status {res.status_code} for {self.user['email']}"
                )
                raise StopUser("Auth API failed")
            self.auth_token = res.json().get("data", {}).get("auth_token")
            if not self.auth_token:
                logger.error(f"❌ Auth token missing for {self.user['email']}")
                raise StopUser("Missing auth_token")
        except StopUser:
            raise
        except Exception as e:
            logger.error(f"❌ Auth exception for {self.user['email']}: {e}")
            raise StopUser("User authentication failed")

    def _navigate_pages(self, report):
        """Navigate standard pages and record timings. Raises StopUser on failure."""
        start = time.time()
        # time.sleep(3)  # throttle before lab start to avoid DB contention
        res = self.client.get("/home-user", name="Dashboard")
        report["Home Page"] = time.time() - start
        if res.status_code != 200:
            logger.error(
                f"❌ Home Page failed for {self.user['email']}: {res.status_code}"
            )
            raise StopUser("Home Page failed")

        start = time.time()
        res = self.client.get("/my-training-user", name="My Training")
        report["My Training"] = time.time() - start
        if res.status_code != 200:
            logger.error(
                f"❌ My Training failed for {self.user['email']}: {res.status_code}"
            )
            raise StopUser("My Training failed")

        start = time.time()
        res = self.client.get("/learn/course/test_whizlabs/3507/", name="Course Page")
        report["Course Page"] = time.time() - start
        if res.status_code != 200 or '<div id="root"></div>' not in res.text:
            logger.error(
                f"❌ Course Page failed for {self.user['email']}: {res.status_code}"
            )
            raise StopUser("Course Page failed")

        start = time.time()
        res = self.client.get(
            "/labs/introduction-to-amazon-elastic-compute-cloud-ec2", name="Lab Page"
        )
        report["EC2 Lab Page"] = time.time() - start
        if res.status_code != 200:
            logger.error(
                f"❌ Lab Page failed for {self.user['email']}: {res.status_code}"
            )
            raise StopUser("Lab Page failed")

    def _start_lab(self, headers, lab_payload, report):
        """Start lab, verify credentials, and update task status."""
        # time.sleep(3)  # throttle before lab start to avoid DB contention
        start = time.time()
        res = self.client.post(
            "https://play.whizlabs.com/api/web/lab/play-create-lab",
            data=json.dumps(lab_payload),
            headers=headers,
            name="Start Lab",
        )
        report["Start Lab"] = time.time() - start
        time.sleep(5)  # allow lab environment to initialise

        try:
            result = res.json()
        except json.JSONDecodeError:
            logger.error(
                f"❌ Start Lab response not JSON for {self.user['email']}: {res}"
            )
            print("LAB Start Error", res)
            raise StopUser("Invalid JSON in Start Lab response")

        data = result.get("data", {})
        if not (
            result.get("status") is True
            and data.get("login_link")
            and data.get("username")
            and data.get("password")
        ):
            logger.warning(
                f"⚠️ Lab started but credentials missing for {self.user['email']}"
            )
            raise StopUser("Lab started but no credentials")

        logger.info(f"✅ Lab started for {self.user['email']}")
        self._update_task_status(headers, lab_payload)

    def _update_task_status(self, headers, lab_payload):
        """Update task status after lab is started. Logs but does not stop on failure."""
        try:
            # time.sleep(3)  # throttle before lab start to avoid DB contention
            res = self.client.post(
                "https://play.whizlabs.com/api/web/lab/play-update-user-task-status",
                data=json.dumps(lab_payload),
                headers=headers,
                name="Update Task Status",
            )
            update_result = res.json()
            if update_result.get("status") is True:
                logger.info(f"✅ Task updated for {self.user['email']}")
            else:
                logger.warning(f"❌ Task update failed for {self.user['email']}")
        except Exception as e:
            logger.error(f"❌ Update Task exception for {self.user['email']}: {e}")

    def _stop_lab(self, headers, report):
        """Wait 4 minutes after lab start, then stop the lab."""
        logger.info(
            f"⏳ Waiting 4 minutes before stopping lab for {self.user['email']}"
        )
        time.sleep(10)

        stop_payload = {
            "task_slug": "introduction-to-amazon-elastic-compute-cloud-ec2",
            "error_id": 1,
            "access_token": self.auth_token,
            "ci": 79,
            "pt": 11,
        }

        start = time.time()
        try:
            res = self.client.post(
                "https://play.whizlabs.com/api/web/lab/play-end-lab",
                data=json.dumps(stop_payload),
                headers=headers,
                name="Stop Lab",
            )
            report["Stop Lab"] = time.time() - start

            try:
                result = res.json()
            except json.JSONDecodeError:
                logger.error(
                    f"❌ Stop Lab response not JSON for {self.user['email']}: {res.text}"
                )
                return

            if result.get("status") is True:
                logger.info(f"✅ Lab stopped for {self.user['email']}")
            else:
                logger.warning(
                    f"⚠️ Lab stop failed for {self.user['email']}: {result}"
                )
        except Exception as e:
            logger.error(f"❌ Stop Lab exception for {self.user['email']}: {e}")

    @task
    def full_user_flow(self):
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.auth_token}",
        }
        lab_payload = {
            "task_slug": "introduction-to-amazon-elastic-compute-cloud-ec2",
            "course_id": 1,
            "access_token": self.auth_token,
            "is_sandbox_3": False,
            "pt": 11,
        }
        report = {}

        try:
            self._navigate_pages(report)
            self._start_lab(headers, lab_payload, report)
            # self._stop_lab(headers, report)
        except StopUser:
            raise
        except Exception as e:
            logger.error(f"💥 Unexpected flow error for {self.user['email']}: {e}")
            raise StopUser()

        # 📊 Session summary
        logger.info("📊 [SESSION SUMMARY]")
        for k, v in report.items():
            logger.info(f"{k:25s} ➤ {v:.2f}s")

        raise StopUser()


# python3 -m locust -f index.py --host https://lexs.trainocate.co.jp
