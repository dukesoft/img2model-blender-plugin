bl_info = {
    "name": "Img2Model",
    "author": "img2model.com",
    "version": (1, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Image 2 Model",
    "description": "Generate 3D models from images",
    "category": "3D View",
}

import os
import tempfile
import threading
import time

# Blender specifics
import bpy
import requests
from bpy.props import StringProperty, BoolProperty, IntProperty, FloatProperty

class Img2modelProperties(bpy.types.PropertyGroup):
    api_key: StringProperty(
        name="API Key",
        description="Your API Key - if you don't have one, create it at img2model.com.",
        default="12345"
    )
    in_progress: BoolProperty(
        name="Processing",
        default=False
    )
    job_id: StringProperty(
        name="Job ID",
        default=""
    )
    status_message: StringProperty(
        name="Status Message",
        default=""
    )
    image_path: StringProperty(
        name="Image",
        description="Select an image to upload",
        subtype='FILE_PATH'
    )
    detail_level: IntProperty(
        name="Detail Level",
        description="Select the detail level required for this model",
        default=3,
        min=1,
        max=5,
    )
    poly_count: IntProperty(
        name="Number of polygons",
        description="Low-poly or high-poly model",
        default=2000,
        min=1000,
        max=100000,
        step=1000
    )
    remove_background: BoolProperty(
        name="Remove Background",
        description="Whether to remove the background from the image",
        default=True
    )
    texture: BoolProperty(
        name="Generate Texture",
        description="Whether to generate texture for the 3D model",
        default=True
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

        layout.prop(props, "detail_level")
        layout.prop(props, "poly_count")

        layout.prop(props, "remove_background")
        layout.prop(props, "texture")

        row = layout.row()
        row.enabled = not props.in_progress
        row.operator("object.generate_3d")

        if props.in_progress:
            if props.status_message:
                for line in props.status_message.split("\n"):
                    layout.label(text=line)
            else:
                layout.label("Processing...")

class Img2modelOperator(bpy.types.Operator):
    bl_idname = "object.generate_3d"
    bl_label = "Generate 3D Model"
    bl_description = "Generate a 3D model from text description, an image or a selected mesh"

    debug = False

    # parameters
    job_id = ''
    api_url = "https://img2model.com/api"
    api_key = ""
    image_path = ""
    detail_level = 3
    poly_count = 2000
    remove_background = False
    texture = False

    # references
    _area = None
    worker = None
    finalized = False

    def modal(self, context, event):
        if event.type in {'RIGHTMOUSE', 'ESC'}:
            return {'CANCELLED'}

        if self.finalized:
            print("Modal finalized")
            self.finalized = False
            context.scene.img2model.in_progress = False

        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        props = context.scene.img2model
        self.api_key = props.api_key
        self.image_path = props.image_path
        self.detail_level = props.detail_level
        self.poly_count = props.poly_count
        self.remove_background = props.remove_background
        self.texture = props.texture
        self._area = context.area

        if not bpy.app.online_access:
            self.report({'WARNING'}, "Online access is disabled in your Blender settings. Internet access is required for this plugin to work.")
            return {'FINISHED'}

        if self.image_path == "":
            self.report({'WARNING'}, "Please select an image first.")
            return {'FINISHED'}

        props.in_progress = True

        # proper filepath handling
        blend_file_dir = os.path.dirname(bpy.data.filepath)
        if self.image_path.startswith('//'):
            self.image_path = self.image_path[2:]
            self.image_path = os.path.join(blend_file_dir, self.image_path)

        if self.debug:
            self.report({'INFO'}, f"filepath={blend_file_dir}")
            self.report({'INFO'}, f"opening file at {self.image_path}")

        props.status_message = f"Submitting job to img2model..."

        self.worker = threading.Thread(target=self.generate_model, args=[context])
        self.worker.start()

        wm = context.window_manager
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def failedState(self, context, message):
        self.report({'ERROR'}, f"Generation failed: {message}")
        self.finalized = True
        props = context.scene.img2model
        props.in_progress = False
        self._area.tag_redraw()
        raise Exception(f'Generation failed: {message}')

    def generate_model(self, context):
        self.report({'INFO'}, f"Submitting job")
        props = context.scene.img2model

        try:
            if not os.path.exists(self.image_path):
                self.report({'ERROR'}, f"Image path does not exist {self.image_path}")
                raise Exception(f'Image path does not exist {self.image_path}')
            self.report({'INFO'}, f"Post Start Image to 3D")

            response = requests.post(
                f"{self.api_url}/jobs/new",
                data={
                    "api_key": self.api_key,
                    "new_job_form[detail]": self.detail_level,
                    "new_job_form[smoothness]": self.poly_count,
                    "new_job_form[auto_texture]": "on" if self.texture else "off",
                    "new_job_form[remove_background]": "on" if self.remove_background else "off",
                    "new_job_form[format]": "glb",
                },
                files={
                    "new_job_form[image]": open(self.image_path, "rb"),
                }
            )

            self.report({'INFO'}, f"Submitted job")
            if self.debug:
                self.report({'INFO'}, f"Result (text): {response.text}")

            if response.status_code != 200:
                return self.failedState(context, f"Error submitting job: {response.status_code}: {response.text}")

            resp_json = response.json()
            if self.debug:
                self.report({'INFO'}, f"Result (decoded): {resp_json}")

            if not resp_json["success"]:
                return self.failedState(context, resp_json["message"])

            # job submitted succesfully, get job ID
            props.status_message = resp_json["message"]
            job_id = resp_json["job_id"]
            self.report({'INFO'}, f"Job ID: {job_id}")

            pstring = "."

            while True:
                time.sleep(1)
                pstring += "."
                if len(pstring) > 3:
                    pstring = "."

                jobstate = requests.post(
                    f"{self.api_url}/jobs/status/{job_id}",
                    data={
                        "api_key": self.api_key,
                    }
                )
                if jobstate.status_code != 200:
                    return self.failedState(context, f"Error checking job state: {jobstate.status_code}: {jobstate.text}")
                jobstate_json = jobstate.json()
                if self.debug:
                    self.report({'INFO'}, f"Result (decoded): {jobstate_json}")

                if not jobstate_json["success"]:
                    msg = jobstate_json["message"]
                    return self.failedState(context, f"Failure checking job state: {msg}")

                if not jobstate_json["finished"]:
                    props.status_message = jobstate_json["state_hr"] + pstring
                    self._area.tag_redraw()
                    continue

                if jobstate_json["state"] == "COMPLETED":
                    return self.handlejobCompleted(context, job_id)

                statemsg = jobstate_json["state_hr"]
                msg = jobstate_json["message"]
                return self.failedState(context, f"Job did not complete: {statemsg} ({msg})")

        except Exception as e:
            return self.failedState(context, f"Error handling job: {e}")

        return self.failedState(context, f"Unhandled job state")

    def handlejobCompleted(self, context, job_id):
        try:
            # download to temp file
            response = requests.post(
                f"{self.api_url}/jobs/result/{job_id}",
                data={
                    "api_key": self.api_key,
                }
            )

            if response.status_code != 200:
                return self.failedState(context, f"Error fetching job data: {str(response.status_code)}: {response.text}")

            tmpfile = tempfile.NamedTemporaryFile(delete=False, suffix=".glb")
            tmpfile.write(response.content)
            tmpfile.close()

            # Import the GLB file in the main worker
            def importFile():
                bpy.ops.import_scene.gltf(filepath=tmpfile.name)
                os.unlink(tmpfile.name)
                return None

            bpy.app.timers.register(importFile)

        except Exception as e:
            self.report({'ERROR'}, f"Error: {str(e)}")

        finally:
            self.finalized = True
            props = context.scene.img2model
            props.in_progress = False

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