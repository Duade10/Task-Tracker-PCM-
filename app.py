from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timedelta

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from task_tracker.database import Task, TaskRepository

BOT_MENTION_PATTERN = re.compile(r"<@([A-Z0-9]+)>")
CHECKBOX_ACTION_PATTERN = re.compile(r"task_checkboxes_(\d+)")


class SlackTaskTracker:
    def __init__(self) -> None:
        self.repo = TaskRepository(os.environ.get("TASK_DB_PATH", "tasks.db"))
        self.app = App(
            token=self._require_env("SLACK_BOT_TOKEN"),
            signing_secret=self._require_env("SLACK_SIGNING_SECRET"),
        )
        self.bot_user_id: str | None = None
        self.tasks_channel = self._require_env("TASKS_CHANNEL")
        self._register_handlers()

    def _require_env(self, key: str) -> str:
        value = os.environ.get(key)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {key}")
        return value

    def _register_handlers(self) -> None:
        @self.app.event("app_mention")
        def handle_app_mention(event, say, client, context):
            self.bot_user_id = context.get("bot_user_id")
            channel = event.get("channel")
            if channel is None:
                return

            text = event.get("text", "")
            author_id = event.get("user")
            developer_id, project_manager_id, summary = self._parse_task_request(text)
            if developer_id is None:
                say(
                    text=(
                        "Please mention a developer when creating a task, "
                        "e.g. `@bot @developer @pm Implement feature`."
                    ),
                    channel=channel,
                )
                return
            if author_id is None:
                say(text="Unable to determine who created the task.", channel=channel)
                return

            title = summary or "New task"
            task = self.repo.create_task(
                title,
                "",
                developer_id,
                project_manager_id,
                self.tasks_channel,
            )
            response = self._post_task_message(client, task)
            if response:
                self.repo.update_message_reference(task.id, response["channel"], response["ts"])
                say(text=f"Task #{task.id} created in <#{self.tasks_channel}>.", channel=channel)

        @self.app.command("/tasks")
        def handle_tasks_command(ack, respond, command):
            ack()
            text = (command.get("text") or "").strip()
            if text.lower().startswith("delete"):
                parts = text.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    respond("Usage: /tasks delete <task_id>")
                    return
                task_id = int(parts[1])
                try:
                    task = self.repo.get_task(task_id)
                except KeyError:
                    respond(f"Task #{task_id} not found.")
                    return

                user_id = command.get("user_id")
                if user_id not in {task.developer_id, task.project_manager_id}:
                    respond(
                        "Only the assigned developer or project manager can delete this task."
                    )
                    return

                try:
                    self.repo.delete_task(task_id)
                except KeyError:
                    respond(f"Task #{task_id} not found.")
                    return

                client = self.app.client
                self._delete_task_message(client, task)
                self._send_channel_notification(
                    client,
                    task.channel_id,
                    f"Task #{task_id} was deleted by <@{user_id}>.",
                )
                respond(f"Task #{task_id} deleted.")
                return
            if text.lower().startswith("show"):
                parts = text.split()
                if len(parts) < 2 or not parts[1].isdigit():
                    respond("Usage: /tasks show <task_id>")
                    return
                task_id = int(parts[1])
                try:
                    task = self.repo.get_task(task_id)
                except KeyError:
                    respond(f"Task #{task_id} not found.")
                    return
                respond(self._format_task_details(task))
                return

            if text == "" or text.lower().startswith("list"):
                trigger_id = command.get("trigger_id")
                if not trigger_id:
                    respond("Unable to open task filters right now. Please try again.")
                    return
                try:
                    self.app.client.views_open(
                        trigger_id=trigger_id,
                        view=self._build_tasks_filter_modal(),
                    )
                except SlackApiError as exc:
                    error = exc.response.get("error") if hasattr(exc, "response") else str(exc)
                    respond(f"Failed to open task filters: {error}")
                return

            status = None
            if text.lower() in {"completed", "complete"}:
                status = "completed"
            elif text.lower() in {"pending", "open", "incomplete"}:
                status = "pending"

            tasks = self.repo.list_tasks(status=status)
            if not tasks:
                respond("No tasks found.")
                return

            blocks: list[dict] = []
            for index, task in enumerate(tasks):
                blocks.extend(self._task_summary_blocks(task))
                if index < len(tasks) - 1:
                    blocks.append({"type": "divider"})

            respond(blocks=blocks)

        @self.app.view("tasks_filter_modal")
        def handle_tasks_filter_submission(ack, body, client, view):
            state_values = view.get("state", {}).get("values", {})
            range_option = self._selected_option_value(state_values, "range_block", "range_select")
            if not range_option:
                ack(
                    response_action="errors",
                    errors={"range_block": "Please choose a date range."},
                )
                return

            start_date = self._date_input_value(state_values, "start_date_block", "start_date_input")
            end_date = self._date_input_value(state_values, "end_date_block", "end_date_input")

            try:
                start_ts, end_ts, range_label = self._calculate_date_range(
                    range_option, start_date, end_date
                )
            except ValueError as exc:
                message = str(exc)
                ack(
                    response_action="errors",
                    errors={
                        "start_date_block": message,
                        "end_date_block": message,
                    },
                )
                return

            status_value = self._selected_option_value(
                state_values, "status_block", "status_select"
            )
            status_label = "All tasks"
            repo_status = None
            if status_value == "completed":
                status_label = "Completed tasks"
                repo_status = "completed"
            elif status_value == "pending":
                status_label = "Pending tasks"
                repo_status = "pending"

            filters = {
                "status": repo_status,
                "status_label": status_label,
                "start": start_ts,
                "end": end_ts,
                "range_label": range_label,
                "page_size": 5,
            }

            tasks, total = self._fetch_tasks_page(filters, 1)
            ack(
                response_action="update",
                view=self._build_tasks_results_view(filters, 1, tasks, total),
            )

        @self.app.action("tasks_results_page")
        def handle_tasks_results_page(ack, body, client):
            ack()
            actions = body.get("actions", [])
            if not actions:
                return
            try:
                page = int(actions[0].get("value", "1"))
            except ValueError:
                return

            metadata_raw = body.get("view", {}).get("private_metadata")
            filters: dict[str, object] | None = None
            if metadata_raw:
                try:
                    filters = json.loads(metadata_raw).get("filters")
                except json.JSONDecodeError:
                    filters = None

            if not isinstance(filters, dict):
                return

            tasks, total = self._fetch_tasks_page(filters, page)
            try:
                client.views_update(
                    view_id=body["view"]["id"],
                    hash=body["view"].get("hash"),
                    view=self._build_tasks_results_view(filters, page, tasks, total),
                )
            except SlackApiError:
                pass

        @self.app.shortcut("create_task_global")
        def handle_global_shortcut(ack, body, client):
            ack()
            trigger_id = body.get("trigger_id")
            if not trigger_id:
                return
            try:
                client.views_open(trigger_id=trigger_id, view=self._build_create_task_modal())
            except SlackApiError as exc:
                print(f"Failed to open create task modal: {exc}")

        @self.app.shortcut("create_task")
        def handle_message_shortcut(ack, body, client):
            ack()
            trigger_id = body.get("trigger_id")
            if not trigger_id:
                return

            message = body.get("message", {})
            initial_description = None
            if isinstance(message, dict):
                text = message.get("text")
                if isinstance(text, str):
                    initial_description = text.strip()

            try:
                client.views_open(
                    trigger_id=trigger_id,
                    view=self._build_create_task_modal(description=initial_description),
                )
            except SlackApiError as exc:
                print(f"Failed to open create task modal: {exc}")

        @self.app.view("create_task_modal")
        def handle_modal_submission(ack, body, client, view):
            ack()
            state_values = view.get("state", {}).get("values", {})

            developer_id = self._selected_user_from_state(
                state_values, "developer_block", "developer_select"
            )
            project_manager_id = self._selected_user_from_state(
                state_values, "pm_block", "pm_select"
            )
            if not developer_id:
                return
            if not project_manager_id:
                project_manager_id = developer_id

            title = self._text_input_value(state_values, "title_block", "title_input") or "New task"
            description = self._text_input_value(
                state_values, "description_block", "description_input"
            ) or ""

            task = self.repo.create_task(
                title.strip(),
                description.strip(),
                developer_id,
                project_manager_id,
                self.tasks_channel,
            )
            response = self._post_task_message(client, task)
            if response:
                self.repo.update_message_reference(task.id, response["channel"], response["ts"])
            self._notify_task_creator(client, body.get("user", {}).get("id"), task)

        @self.app.action(CHECKBOX_ACTION_PATTERN)
        def handle_checkbox_action(ack, body, client):
            ack()
            action = body["actions"][0]
            match = CHECKBOX_ACTION_PATTERN.match(action["action_id"])
            if not match:
                return
            task_id = int(match.group(1))
            task = self.repo.get_task(task_id)

            selected_values = {opt["value"] for opt in action.get("selected_options", [])}
            developer_checked = f"{task_id}|developer" in selected_values
            project_manager_checked = f"{task_id}|pm" in selected_values

            user_id = body["user"]["id"]
            previous_developer_checked = task.developer_checked
            previous_pm_checked = task.project_manager_checked

            if developer_checked != previous_developer_checked and user_id != task.developer_id:
                self._send_ephemeral(
                    client,
                    task.channel_id,
                    user_id,
                    "Only the assigned developer can toggle their checkbox.",
                )
                developer_checked = previous_developer_checked
            if project_manager_checked != previous_pm_checked and user_id != task.project_manager_id:
                self._send_ephemeral(
                    client,
                    task.channel_id,
                    user_id,
                    "Only the project manager can toggle their checkbox.",
                )
                project_manager_checked = previous_pm_checked

            updated_task = self.repo.update_checkmarks(task_id, developer_checked, project_manager_checked)
            self._update_task_message(client, updated_task)

            if developer_checked and not previous_developer_checked:
                self._send_channel_notification(
                    client,
                    updated_task.channel_id,
                    f"Developer <@{task.developer_id}> marked task #{task_id} complete.",
                )
            if project_manager_checked and not previous_pm_checked:
                self._send_channel_notification(
                    client,
                    updated_task.channel_id,
                    f"Project manager <@{task.project_manager_id}> approved task #{task_id}.",
                )
            if updated_task.completed_at and not task.completed_at:
                self._send_channel_notification(
                    client,
                    updated_task.channel_id,
                    f"Task #{task_id} is now fully completed.",
                )

    def _send_ephemeral(self, client: WebClient, channel: str, user: str, text: str) -> None:
        try:
            client.chat_postEphemeral(channel=channel, user=user, text=text)
        except SlackApiError:
            pass

    def _send_channel_notification(self, client: WebClient, channel: str, text: str) -> None:
        try:
            client.chat_postMessage(channel=channel, text=text)
        except SlackApiError:
            pass

    def _delete_task_message(self, client: WebClient, task: Task) -> None:
        if not task.message_ts:
            return
        try:
            client.chat_delete(channel=task.channel_id, ts=task.message_ts)
        except SlackApiError as exc:
            print(f"Failed to delete task message: {exc}")

    def _parse_task_request(
        self, text: str
    ) -> tuple[str | None, str | None, str]:
        mention_ids = BOT_MENTION_PATTERN.findall(text)
        description = BOT_MENTION_PATTERN.sub("", text).strip()

        assignee_mentions: list[str] = []
        for user_id in mention_ids:
            if user_id == self.bot_user_id:
                continue
            assignee_mentions.append(user_id)

        developer_id = assignee_mentions[0] if assignee_mentions else None
        if len(assignee_mentions) > 1:
            project_manager_id = assignee_mentions[1]
        else:
            project_manager_id = developer_id

        return developer_id, project_manager_id, description

    def _build_tasks_filter_modal(self) -> dict:
        range_options = [
            {
                "text": {"type": "plain_text", "text": "Today"},
                "value": "today",
            },
            {
                "text": {"type": "plain_text", "text": "Yesterday"},
                "value": "yesterday",
            },
            {
                "text": {"type": "plain_text", "text": "Last 7 days"},
                "value": "last_7_days",
            },
            {
                "text": {"type": "plain_text", "text": "Custom range"},
                "value": "custom",
            },
        ]

        status_options = [
            {
                "text": {"type": "plain_text", "text": "All tasks"},
                "value": "all",
            },
            {
                "text": {"type": "plain_text", "text": "Pending tasks"},
                "value": "pending",
            },
            {
                "text": {"type": "plain_text", "text": "Completed tasks"},
                "value": "completed",
            },
        ]

        return {
            "type": "modal",
            "callback_id": "tasks_filter_modal",
            "title": {"type": "plain_text", "text": "Filter tasks"},
            "submit": {"type": "plain_text", "text": "Apply"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "Select a date range and (optionally) a status filter. "
                            "Results appear in pages of five tasks."
                        ),
                    },
                },
                {
                    "type": "input",
                    "block_id": "range_block",
                    "label": {"type": "plain_text", "text": "Date range"},
                    "element": {
                        "type": "static_select",
                        "action_id": "range_select",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Choose a range",
                        },
                        "initial_option": range_options[0],
                        "options": range_options,
                    },
                },
                {
                    "type": "input",
                    "block_id": "start_date_block",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Start date"},
                    "element": {
                        "type": "datepicker",
                        "action_id": "start_date_input",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "YYYY-MM-DD",
                        },
                    },
                },
                {
                    "type": "input",
                    "block_id": "end_date_block",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "End date"},
                    "element": {
                        "type": "datepicker",
                        "action_id": "end_date_input",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "YYYY-MM-DD",
                        },
                    },
                },
                {
                    "type": "input",
                    "block_id": "status_block",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Task status"},
                    "element": {
                        "type": "static_select",
                        "action_id": "status_select",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "All tasks",
                        },
                        "initial_option": status_options[0],
                        "options": status_options,
                    },
                },
            ],
        }

    def _build_create_task_modal(
        self,
        developer_id: str | None = None,
        project_manager_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
    ) -> dict:
        developer_element: dict[str, object] = {
            "type": "users_select",
            "action_id": "developer_select",
            "placeholder": {"type": "plain_text", "text": "Select a developer"},
        }
        if developer_id:
            developer_element["initial_user"] = developer_id

        pm_element: dict[str, object] = {
            "type": "users_select",
            "action_id": "pm_select",
            "placeholder": {"type": "plain_text", "text": "Select a project manager"},
        }
        if project_manager_id:
            pm_element["initial_user"] = project_manager_id

        title_element: dict[str, object] = {
            "type": "plain_text_input",
            "action_id": "title_input",
            "placeholder": {"type": "plain_text", "text": "Enter a short title"},
        }
        if title:
            title_element["initial_value"] = title

        description_element: dict[str, object] = {
            "type": "plain_text_input",
            "multiline": True,
            "action_id": "description_input",
            "placeholder": {
                "type": "plain_text",
                "text": "Provide additional details (optional)",
            },
        }
        if description:
            description_element["initial_value"] = description

        return {
            "type": "modal",
            "callback_id": "create_task_modal",
            "title": {"type": "plain_text", "text": "Create Task"},
            "submit": {"type": "plain_text", "text": "Create"},
            "close": {"type": "plain_text", "text": "Cancel"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "developer_block",
                    "label": {"type": "plain_text", "text": "Developer"},
                    "element": developer_element,
                },
                {
                    "type": "input",
                    "block_id": "pm_block",
                    "label": {"type": "plain_text", "text": "Project manager"},
                    "element": pm_element,
                },
                {
                    "type": "input",
                    "block_id": "title_block",
                    "label": {"type": "plain_text", "text": "Task title"},
                    "element": title_element,
                },
                {
                    "type": "input",
                    "block_id": "description_block",
                    "optional": True,
                    "label": {"type": "plain_text", "text": "Description"},
                    "element": description_element,
                },
            ],
        }

    def _selected_user_from_state(self, values: dict, block_id: str, action_id: str) -> str | None:
        block = values.get(block_id, {})
        element = block.get(action_id, {})
        return element.get("selected_user")

    def _text_input_value(self, values: dict, block_id: str, action_id: str) -> str | None:
        block = values.get(block_id, {})
        element = block.get(action_id, {})
        value = element.get("value")
        if isinstance(value, str):
            return value
        return None

    def _selected_option_value(
        self, values: dict, block_id: str, action_id: str
    ) -> str | None:
        block = values.get(block_id, {})
        element = block.get(action_id, {})
        option = element.get("selected_option")
        if isinstance(option, dict):
            value = option.get("value")
            if isinstance(value, str):
                return value
        return None

    def _date_input_value(self, values: dict, block_id: str, action_id: str) -> str | None:
        block = values.get(block_id, {})
        element = block.get(action_id, {})
        value = element.get("selected_date")
        if isinstance(value, str) and value:
            return value
        return None

    def _calculate_date_range(
        self, option: str, start_date: str | None, end_date: str | None
    ) -> tuple[str, str, str]:
        today = datetime.utcnow().date()

        def to_iso(day) -> str:
            return datetime.combine(day, datetime.min.time()).strftime("%Y-%m-%dT%H:%M:%S")

        if option == "today":
            start = today
            end = today + timedelta(days=1)
            label = "Today"
        elif option == "yesterday":
            start = today - timedelta(days=1)
            end = today
            label = "Yesterday"
        elif option == "last_7_days":
            start = today - timedelta(days=6)
            end = today + timedelta(days=1)
            label = "Last 7 days"
        elif option == "custom":
            if not start_date or not end_date:
                raise ValueError("Select both a start and end date for a custom range.")
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
            if end_dt < start_dt:
                raise ValueError("End date must be after the start date.")
            start = start_dt
            end = end_dt + timedelta(days=1)
            label = (
                "Custom range: "
                f"{start_dt.strftime('%b %d, %Y')} — {end_dt.strftime('%b %d, %Y')}"
            )
        else:
            start = today - timedelta(days=6)
            end = today + timedelta(days=1)
            label = "Last 7 days"

        return to_iso(start), to_iso(end), label

    def _fetch_tasks_page(
        self, filters: dict[str, object], page: int
    ) -> tuple[list[Task], int]:
        page_size = int(filters.get("page_size", 5) or 5)
        page = max(page, 1)
        offset = (page - 1) * page_size
        status = filters.get("status")
        start = filters.get("start")
        end = filters.get("end")
        tasks = self.repo.list_tasks(
            status=status if isinstance(status, str) else None,
            start=start if isinstance(start, str) else None,
            end=end if isinstance(end, str) else None,
            limit=page_size,
            offset=offset,
        )
        total = self.repo.count_tasks(
            status=status if isinstance(status, str) else None,
            start=start if isinstance(start, str) else None,
            end=end if isinstance(end, str) else None,
        )
        return tasks, total

    def _build_tasks_results_view(
        self, filters: dict[str, object], page: int, tasks: list[Task], total: int
    ) -> dict:
        page_size = int(filters.get("page_size", 5) or 5)
        total_pages = max(1, math.ceil(total / page_size)) if total else 1
        page = max(1, min(page, total_pages))
        start_index = (page - 1) * page_size + 1 if total else 0
        end_index = min(page * page_size, total) if total else 0

        status_label = filters.get("status_label") or "All tasks"
        range_label = filters.get("range_label") or "Last 7 days"

        blocks: list[dict] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{status_label}* — {range_label}",
                },
            }
        ]

        if total:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"Showing {start_index}-{end_index} of {total} tasks (page {page} of {total_pages}).",
                        }
                    ],
                }
            )

            for index, task in enumerate(tasks):
                blocks.extend(self._task_summary_blocks(task))
                if index < len(tasks) - 1:
                    blocks.append({"type": "divider"})

            nav_elements: list[dict] = []
            if page > 1:
                nav_elements.append(
                    {
                        "type": "button",
                        "action_id": "tasks_results_page",
                        "text": {"type": "plain_text", "text": "Previous"},
                        "value": str(page - 1),
                    }
                )
            if page < total_pages:
                nav_elements.append(
                    {
                        "type": "button",
                        "action_id": "tasks_results_page",
                        "text": {"type": "plain_text", "text": "Next"},
                        "value": str(page + 1),
                    }
                )
            if nav_elements:
                blocks.append({"type": "actions", "elements": nav_elements})
        else:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "No tasks match the selected filters.",
                    },
                }
            )

        return {
            "type": "modal",
            "callback_id": "tasks_results_modal",
            "title": {"type": "plain_text", "text": "Tasks"},
            "close": {"type": "plain_text", "text": "Close"},
            "blocks": blocks,
            "private_metadata": json.dumps({"filters": filters}),
        }

    def _notify_task_creator(self, client: WebClient, user_id: str | None, task: Task) -> None:
        if not user_id:
            return
        try:
            dm_response = client.conversations_open(users=user_id)
            channel_id = dm_response.get("channel", {}).get("id")
            if not channel_id:
                return
            client.chat_postMessage(
                channel=channel_id,
                text=f"Task #{task.id} created in <#{task.channel_id}>.",
            )
        except SlackApiError:
            pass

    def _task_summary_blocks(self, task: Task) -> list[dict]:
        status_label = ":white_check_mark: Completed" if task.completed_at else ":hourglass_flowing_sand: Pending"
        developer_status = (
            ":white_check_mark: Done" if task.developer_checked else ":hourglass_flowing_sand: Pending"
        )
        project_manager_status = (
            ":white_check_mark: Approved"
            if task.project_manager_checked
            else ":hourglass_flowing_sand: Awaiting review"
        )

        timeline_elements = [
            {"type": "mrkdwn", "text": f":calendar: Created {self._format_timestamp(task.created_at)}"}
        ]
        if task.completed_at:
            timeline_elements.append(
                {
                    "type": "mrkdwn",
                    "text": f":checkered_flag: Completed {self._format_timestamp(task.completed_at)}",
                }
            )

        description_text = None
        if task.description and task.description.strip():
            desc = task.description.strip()
            if desc != task.title.strip():
                description_text = desc

        summary_text = f"{status_label}\n*{task.title}*"
        if description_text:
            summary_text = f"{summary_text}\n{description_text}"

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Task #{task.id}", "emoji": True},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": summary_text},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Developer*\n<@{task.developer_id}>"},
                    {"type": "mrkdwn", "text": f"*Project manager*\n<@{task.project_manager_id}>"},
                ],
            },
            {"type": "context", "elements": timeline_elements},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Developer status*\n{developer_status}"},
                    {"type": "mrkdwn", "text": f"*PM status*\n{project_manager_status}"},
                ],
            },
        ]

        return blocks

    def _format_task_details(self, task: Task) -> str:
        lines = [
            f"*Task #{task.id}*",
            f"Status: {'Completed' if task.completed_at else 'Pending'}",
            f"Developer: <@{task.developer_id}>",
            f"Project manager: <@{task.project_manager_id}>",
            f"Created: {self._format_timestamp(task.created_at)}",
        ]
        if task.completed_at:
            lines.append(f"Completed: {self._format_timestamp(task.completed_at)}")
        lines.append("")
        lines.append(task.title)
        if task.description and task.description.strip() and task.description.strip() != task.title.strip():
            lines.append("")
            lines.append(task.description)
        return "\n".join(lines)

    def _format_timestamp(self, ts: str | None) -> str:
        if not ts:
            return "-"
        try:
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            return dt.strftime("%b %d, %Y %H:%M UTC")
        except ValueError:
            return ts

    def _build_task_blocks(self, task: Task) -> list[dict]:
        checkbox_options = [
            {
                "text": {"type": "mrkdwn", "text": f"Developer complete (<@{task.developer_id}>)"},
                "value": f"{task.id}|developer",
            },
            {
                "text": {
                    "type": "mrkdwn",
                    "text": f"Project manager approved (<@{task.project_manager_id}>)",
                },
                "value": f"{task.id}|pm",
            },
        ]

        checkboxes_element = {
            "type": "checkboxes",
            "action_id": f"task_checkboxes_{task.id}",
            "options": checkbox_options,
        }

        initial_options: list[dict] = []
        if task.developer_checked:
            initial_options.append(checkbox_options[0])
        if task.project_manager_checked:
            initial_options.append(checkbox_options[1])

        if initial_options:
            checkboxes_element["initial_options"] = initial_options

        summary_blocks = self._task_summary_blocks(task)
        extra_blocks = [
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": ":white_check_mark: Completion checklist"}],
            },
            {
                "type": "actions",
                "block_id": f"task-{task.id}-actions",
                "elements": [checkboxes_element],
            },
        ]

        return summary_blocks + extra_blocks

    def _post_task_message(self, client: WebClient, task: Task) -> dict | None:
        try:
            response = client.chat_postMessage(
                channel=self.tasks_channel,
                text=f"Task #{task.id}",
                blocks=self._build_task_blocks(task),
            )
            return {
                "channel": response.get("channel"),
                "ts": response.get("ts"),
            }
        except SlackApiError as exc:
            print(f"Failed to post task message: {exc}")
            return None

    def _update_task_message(self, client: WebClient, task: Task) -> None:
        if not task.message_ts:
            return
        try:
            client.chat_update(
                channel=task.channel_id,
                ts=task.message_ts,
                text=f"Task #{task.id}",
                blocks=self._build_task_blocks(task),
            )
        except SlackApiError as exc:
            print(f"Failed to update task message: {exc}")

    def start(self) -> None:
        handler = SocketModeHandler(self.app, self._require_env("SLACK_APP_TOKEN"))
        handler.start()


def main() -> None:
    SlackTaskTracker().start()


if __name__ == "__main__":
    main()

