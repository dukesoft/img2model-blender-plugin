bl_info = {
    "name": "Img2Model",
    "author": "img2model.com",
    "version": (1, 1),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Image 2 Model",
    "description": "Generate 3D models from images",
    "category": "3D View",
}

import html
import os
import re
import tempfile
import threading
import time

# Blender specifics
import bpy
import requests
from bpy.props import StringProperty, BoolProperty, IntProperty, EnumProperty

API_URL_DEFAULT = "https://img2model.com/api"

# The API asks for 3-10 seconds between status calls; hammering it does not make
# the GPU any faster.
POLL_INTERVAL = 3.0

# How often the modal operator copies worker state into the UI.
UI_INTERVAL = 0.25

# new_job_form[mode] - a mode is a name for the texture settings, and it wins
# over the individual switches it stands for, so it is the only texture field we
# send.
MODE_ITEMS = [
    ('full_pbr', "Full PBR",
     "Measured metallic and roughness on top of the colour. The best materials, and about twice the wait"),
    ('simple_pbr', "Basic PBR",
     "Colour plus a metallic/roughness map. The fastest way to a usable material"),
    ('albedo_only', "Basic",
     "Colour only, no material map. The honest option for photographs, where a guessed metallic map reads as dull chrome"),
    ('textureless', "Textureless",
     "Bare geometry, no colour at all. The fastest and cheapest option, and what you want for 3D printing"),
]

# new_job_form[detail] - how hard the generator works on the shape.
DETAIL_ITEMS = [
    ('1', "Draft", "Roughest shape, cheapest and quickest"),
    ('2', "Fast", "Below standard detail"),
    ('3', "Standard", "The default"),
    ('4', "High", "More shape detail, longer wait"),
    ('5', "Ultra", "The most shape detail, and the longest wait"),
]

# new_job_form[texture_size] - a step, not a pixel count.
TEXTURE_SIZE_ITEMS = [
    ('1', "1024 x 1024", "1024 x 1024 texture"),
    ('2', "2048 x 2048", "2048 x 2048 texture"),
    ('3', "4096 x 4096", "4096 x 4096 texture"),
]


def plain_text(message):
    """API messages may carry HTML line breaks when they list validation errors."""
    if not message:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", str(message))
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def resolve_image_path(image_path):
    """Turn a Blender-relative // path into something open() understands."""
    if image_path.startswith('//'):
        return os.path.join(os.path.dirname(bpy.data.filepath), image_path[2:])
    return image_path


class Img2modelProperties(bpy.types.PropertyGroup):
    api_key: StringProperty(
        name="API Key",
        description="Your API Key - if you don't have one, create it at img2model.com",
        default="",
        subtype='PASSWORD'
    )
    api_url: StringProperty(
        name="API URL",
        description="Base URL of the img2model API. Only change this if you were told to",
        default=API_URL_DEFAULT
    )
    in_progress: BoolProperty(
        name="Processing",
        default=False
    )
    status_message: StringProperty(
        name="Status Message",
        default=""
    )
    image_path: StringProperty(
        name="Image",
        description="Select an image to upload (png, jpeg, gif or webp, up to 20 MB)",
        subtype='FILE_PATH'
    )
    mode: EnumProperty(
        name="Model Type",
        description="What kind of model you want - the one setting to change if you only change one",
        items=MODE_ITEMS,
        default='simple_pbr'
    )
    detail_level: EnumProperty(
        name="Detail Level",
        description="How hard the generator works on the shape. The single biggest lever on both price and wait",
        items=DETAIL_ITEMS,
        default='3'
    )
    poly_count: IntProperty(
        name="Polygons",
        description="Target face count of the delivered mesh. It is a target, not a promise - results land within a few percent",
        default=25000,
        min=1000,
        max=1000000,
        step=1000
    )
    texture_size: EnumProperty(
        name="Texture Size",
        description="Resolution of the generated texture. Your plan caps this",
        items=TEXTURE_SIZE_ITEMS,
        default='2'
    )


class Img2modelPanel(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'img2model'
    bl_label = 'img2model Generator'

    def draw(self, context):
        layout = self.layout
        props = context.scene.img2model

        layout.prop(props, "api_key")
        layout.prop(props, "image_path")

        layout.separator()

        layout.prop(props, "mode")
        layout.prop(props, "detail_level")
        layout.prop(props, "poly_count")

        row = layout.row()
        row.enabled = props.mode != 'textureless'
        row.prop(props, "texture_size")

        layout.separator()

        row = layout.row()
        row.enabled = not props.in_progress
        row.operator("object.generate_3d", icon='SHADERFX')

        if props.in_progress:
            box = layout.box()
            for line in (props.status_message or "Processing...").split("\n"):
                box.label(text=line)
            box.label(text="Press ESC to stop watching this job", icon='INFO')

        header, body = layout.panel("img2model_advanced", default_closed=True)
        header.label(text="Advanced")
        if body:
            body.prop(props, "api_url")


class Img2modelOperator(bpy.types.Operator):
    bl_idname = "object.generate_3d"
    bl_label = "Generate 3D Model"
    bl_description = "Generate a 3D model from the selected image"

    debug = False

    def invoke(self, context, event):
        props = context.scene.img2model

        if not bpy.app.online_access:
            self.report({'WARNING'}, "Online access is disabled in your Blender settings. "
                                     "Internet access is required for this plugin to work.")
            return {'CANCELLED'}

        if props.in_progress:
            self.report({'WARNING'}, "A job is already running.")
            return {'CANCELLED'}

        if not props.api_key.strip():
            self.report({'WARNING'}, "Please enter your API key. You can create one at img2model.com.")
            return {'CANCELLED'}

        if not props.image_path:
            self.report({'WARNING'}, "Please select an image first.")
            return {'CANCELLED'}

        image_path = resolve_image_path(props.image_path)
        if not os.path.isfile(image_path):
            self.report({'WARNING'}, f"Image does not exist: {image_path}")
            return {'CANCELLED'}

        # Everything the worker thread needs, read on the main thread. The
        # thread never touches bpy - it only writes the plain attributes below,
        # which modal() copies into the properties.
        self._api_url = props.api_url.strip().rstrip('/') or API_URL_DEFAULT
        self._api_key = props.api_key.strip()
        self._image_path = image_path
        self._poly_count = props.poly_count
        self._fields = {
            "new_job_form[mode]": props.mode,
            "new_job_form[detail]": props.detail_level,
            "new_job_form[polycount]": str(props.poly_count),
            "new_job_form[format]": "glb",
        }
        # texture_size is ignored for a job without textures.
        if props.mode != 'textureless':
            self._fields["new_job_form[texture_size]"] = props.texture_size

        self._status = "Submitting job to img2model..."
        self._error = None
        self._info = None
        self._credits = None
        self._result_file = None
        self._done = False
        self._cancelled = False
        self._area = context.area

        props.in_progress = True
        props.status_message = self._status

        if self.debug:
            print(f"[img2model] posting {self._fields} with {self._image_path}")

        self._worker = threading.Thread(target=self.run_job, daemon=True)
        self._worker.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(UI_INTERVAL, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        props = context.scene.img2model

        if event.type in {'RIGHTMOUSE', 'ESC'}:
            self._cancelled = True
            self.report({'INFO'}, "Stopped watching the job. It keeps running on img2model.com.")
            self.discard_result()
            return self.finish(context)

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        if props.status_message != self._status:
            props.status_message = self._status
            if self._area:
                self._area.tag_redraw()

        if not self._done:
            return {'PASS_THROUGH'}

        if self._error:
            self.report({'ERROR'}, self._error)
        else:
            # Importing touches bpy, so it happens here on the main thread.
            try:
                bpy.ops.import_scene.gltf(filepath=self._result_file)
                summary = self._info or "Model imported."
                if self._credits is not None:
                    summary += f" {self._credits} credits left."
                self.report({'INFO'}, summary)
            except Exception as exception:
                self.report({'ERROR'}, f"Could not import the generated model: {exception}")
            finally:
                self.discard_result()

        return self.finish(context)

    def discard_result(self):
        """Remove the downloaded file, once it has been imported or given up on."""
        if not self._result_file:
            return
        try:
            os.unlink(self._result_file)
        except OSError:
            pass
        self._result_file = None

    def finish(self, context):
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None

        props = context.scene.img2model
        props.in_progress = False
        props.status_message = ""
        if self._area:
            self._area.tag_redraw()

        return {'FINISHED'}

    def fail(self, message):
        """Called from the worker thread only."""
        self._error = plain_text(message)
        self._done = True

    def api_post(self, path, **kwargs):
        return requests.post(f"{self._api_url}/{path}", timeout=120, **kwargs)

    def run_job(self):
        """Runs on the worker thread. Must not touch bpy."""
        try:
            job_id = self.submit_job()
            if job_id is None:
                return

            if not self.wait_for_job(job_id):
                return

            self.download_result(job_id)

        except requests.RequestException as exception:
            self.fail(f"Could not reach the img2model API: {exception}")
        except Exception as exception:
            self.fail(f"Error handling job: {exception}")

    def submit_job(self):
        """Returns the job id, or None once the failure has been recorded."""
        with open(self._image_path, "rb") as image:
            response = self.api_post(
                "jobs/new",
                data=dict(self._fields, api_key=self._api_key),
                files={"new_job_form[image]": image},
            )

        # A non-2xx status is a transport problem; `success` is the verdict.
        if response.status_code != 200:
            self.fail(f"Error submitting job ({response.status_code}): {response.text}")
            return None

        result = response.json()
        if self.debug:
            print(f"[img2model] submit response: {result}")

        if not result.get("success"):
            self.fail(result.get("message") or "The job was refused.")
            return None

        # Kept for the summary at the end - the first poll overwrites _status.
        self._credits = result.get("remaining_credits")
        self._status = "Job accepted, waiting for a GPU..."

        return result.get("job_id")

    def wait_for_job(self, job_id):
        """Blocks until the job stops changing. False once a failure is recorded."""
        waited = 0
        while not self._cancelled:
            time.sleep(POLL_INTERVAL)
            waited += POLL_INTERVAL

            response = self.api_post(f"jobs/status/{job_id}", data={"api_key": self._api_key})
            if response.status_code != 200:
                self.fail(f"Error checking job state ({response.status_code}): {response.text}")
                return False

            state = response.json()
            if self.debug:
                print(f"[img2model] status response: {state}")

            # When the status endpoint rejects the key it answers in the
            # submission shape, so `finished` is missing rather than null -
            # success has to be checked before anything else.
            if not state.get("success"):
                self.fail(state.get("message") or "Could not read the job state.")
                return False

            # `finished` is true for FAILED and REJECTED as well, so the loop
            # exits on it and the state is judged separately.
            if not state.get("finished"):
                self._status = f"{state.get('state_hr') or 'Working'}... ({int(waited)}s)"
                continue

            if state.get("state") == "COMPLETED":
                faces = state.get("faces")
                if faces and faces < self._poly_count * 0.5:
                    self._info = (f"Generated {faces} faces, well short of the {self._poly_count} requested - "
                                  f"the result may be thin.")
                self._status = "Downloading model..."
                return True

            self.fail(f"Job did not complete: {state.get('state_hr') or state.get('state')} "
                      f"({plain_text(state.get('message')) or 'no reason given'})")
            return False

        return False

    def download_result(self, job_id):
        response = self.api_post(f"jobs/result/{job_id}", data={"api_key": self._api_key})

        if response.status_code != 200:
            self.fail(f"Error fetching job result ({response.status_code}): {response.text}")
            return

        # A job that is not finished - or not yours - gets a JSON refusal
        # instead of the model, so check before writing the body to disk.
        if response.content[:1] == b"{":
            try:
                message = response.json().get("message")
            except ValueError:
                message = None
            self.fail(message or "The API refused to hand over the result.")
            return

        with tempfile.NamedTemporaryFile(delete=False, suffix=".glb") as tmpfile:
            tmpfile.write(response.content)
            self._result_file = tmpfile.name

        self._done = True


classes = (
    Img2modelProperties,
    Img2modelOperator,
    Img2modelPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.img2model = bpy.props.PointerProperty(type=Img2modelProperties)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.img2model


if __name__ == "__main__":
    register()
