"""The central program that ties all the modules together."""

import tensorflow as tf
import numpy as np
import cv2
import win32pipe, win32file
from src.common import config, utils
import mss

def load_model():
    """
    Loads the saved model's weights into an Tensorflow model.
    :return:    The Tensorflow model object.
    """

    model_dir = f'assets/models/rune_model_rnn_filtered_cannied/saved_model'
    return tf.saved_model.load(model_dir)


def canny(image):
    """
    Performs Canny edge detection on IMAGE.
    :param image:   The input image as a Numpy array.
    :return:        The edges in IMAGE.
    """

    image = cv2.Canny(image, 200, 300)
    colored = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    return colored


def filter_color(image):
    """
    Filters out all colors not between orange and green on the HSV scale, which
    eliminates some noise around the arrows.
    :param image:   The input image.
    :return:        The color-filtered image.
    """

    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (1, 100, 100), (75, 255, 255))

    # Mask the image
    color_mask = mask > 0
    arrows = np.zeros_like(image, np.uint8)
    arrows[color_mask] = image[color_mask]
    return arrows


def run_inference_for_single_image(model, image):
    """
    Performs an inference once.
    :param model:   The model object to use.
    :param image:   The input image.
    :return:        The model's predictions including bounding boxes and classes.
    """

    image = np.asarray(image)

    input_tensor = tf.convert_to_tensor(image)
    input_tensor = input_tensor[tf.newaxis,...]

    model_fn = model.signatures['serving_default']
    output_dict = model_fn(input_tensor)

    num_detections = int(output_dict.pop('num_detections'))
    output_dict = {key: value[0,:num_detections].numpy()
                   for key, value in output_dict.items()}
    output_dict['num_detections'] = num_detections
    output_dict['detection_classes'] = output_dict['detection_classes'].astype(np.int64)
    return output_dict


def sort_by_confidence(model, image):
    """
    Runs a single inference on the image and returns the best four classifications.
    :param model:   The model object to use.
    :param image:   The input image.
    :return:        The model's top four predictions.
    """

    output_dict = run_inference_for_single_image(model, image)
    zipped = list(zip(output_dict['detection_scores'],
                      output_dict['detection_boxes'],
                      output_dict['detection_classes']))
    pruned = [t for t in zipped if t[0] > 0.5]
    pruned.sort(key=lambda x: x[0], reverse=True)
    result = pruned[:4]
    return result


def get_boxes(model, image):
    """
    Returns the bounding boxes of the top four classified arrows.
    :param model:   The model object to predict with.
    :param image:   The input image.
    :return:        Up to four bounding boxes.
    """

    output_dict = run_inference_for_single_image(model, image)
    zipped = list(zip(output_dict['detection_scores'],
                      output_dict['detection_boxes'],
                      output_dict['detection_classes']))
    pruned = [t for t in zipped if t[0] > 0.5]
    pruned.sort(key=lambda x: x[0], reverse=True)
    pruned = pruned[:4]
    boxes = [t[1:] for t in pruned]
    return boxes

@utils.run_if_enabled
def merge_detection(model, image):
    """
    Run two inferences: one on the upright image, and one on the image rotated 90 degrees.
    Only considers vertical arrows and merges the results of the two inferences together.
    (Vertical arrows in the rotated image are actually horizontal arrows).
    :param model:   The model object to use.
    :param image:   The input image.
    :return:        A list of four arrow directions.
    """

    label_map = {1: 'up', 2: 'down', 3: 'left', 4: 'right'}
    converter = {'up': 'right', 'down': 'left'}         # For the 'rotated inferences'
    classes = []

    # Preprocessing
    height, width, channels = image.shape
    cropped = image[120:height//2, width//4:3*width//4]
    filtered = filter_color(cropped)
    cannied = canny(filtered)

    # Isolate the rune box
    height, width, channels = cannied.shape
    boxes = get_boxes(model, cannied)
    if len(boxes) == 4:      # Only run further inferences if arrows have been correctly detected
        y_mins = [b[0][0] for b in boxes]
        x_mins = [b[0][1] for b in boxes]
        y_maxes = [b[0][2] for b in boxes]
        x_maxes = [b[0][3] for b in boxes]
        left = int(round(min(x_mins) * width))
        right = int(round(max(x_maxes) * width))
        top = int(round(min(y_mins) * height))
        bottom = int(round(max(y_maxes) * height))
        rune_box = cannied[top:bottom, left:right]

        # Pad the rune box with black borders, effectively eliminating the noise around it
        height, width, channels = rune_box.shape
        pad_height, pad_width = 384, 455
        preprocessed = np.full((pad_height, pad_width, channels), (0, 0, 0), dtype=np.uint8)
        x_offset = (pad_width - width) // 2
        y_offset = (pad_height - height) // 2

        if x_offset > 0 and y_offset > 0:
            preprocessed[y_offset:y_offset+height, x_offset:x_offset+width] = rune_box

        # Run detection on preprocessed image
        lst = sort_by_confidence(model, preprocessed)
        lst.sort(key=lambda x: x[1][1])
        classes = [label_map[item[2]] for item in lst]

        # Run detection on rotated image
        rotated = cv2.rotate(preprocessed, cv2.ROTATE_90_COUNTERCLOCKWISE)
        lst = sort_by_confidence(model, rotated)
        lst.sort(key=lambda x: x[1][2], reverse=True)
        rotated_classes = [converter[label_map[item[2]]]
                           for item in lst
                           if item[2] in [1, 2]]

        # Merge the two detection results
        for i in range(len(classes)):
            if rotated_classes and classes[i] in ['left', 'right']:
                classes[i] = rotated_classes.pop(0)

    return classes


config.enabled = True
monitor = {'top': 0, 'left': 0, 'width': 1366, 'height': 768}
model = load_model()

class PipeServer():
    def __init__(self, pipeName):
        self.pipe = win32pipe.CreateNamedPipe(
            r'\\.\pipe\\'+pipeName,
            win32pipe.PIPE_ACCESS_OUTBOUND,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
            1, 65536, 65536,
            0,
            None)

    #Carefull, this blocks until a connection is established
    def connect(self):
        win32pipe.ConnectNamedPipe(self.pipe, None)

    #Message without tailing '\n'
    def write(self, message):
        win32file.WriteFile(self.pipe, message.encode()+b'\n')

    def close(self):
        win32file.CloseHandle(self.pipe)

""" ENTRY POINT """

# Run detection once to load models beforehand
print("Running detection pre-stage")
with mss.mss() as sct:
    frame = np.array(sct.grab(monitor))
    arrows = merge_detection(model, frame)

print(f"Detected [{len(arrows)}] arrows:", )

# Main loop (server via named pipe)
while True:
    # Blocks logic
    print("Listening for incoming connection")
    t = PipeServer("RuneSolverServer")
    t.connect()

    arrows = []

    current_attempts = 0
    max_attempts = 100

    while True:
        with mss.mss() as sct:
            frame = np.array(sct.grab(monitor))
            arrows = merge_detection(model, frame)

            print(f"Detected [{len(arrows)}] arrows:", )
            print(arrows)

            if(len(arrows) == 4):
                break
            elif(current_attempts < max_attempts):
                print(f"Retrying. Attempt {current_attempts} / {max_attempts}:")
                current_attempts += 1
            else:
                print(f"Max attempts reached: {current_attempts} / {max_attempts}:")
                break
            # time.sleep(0.01)

    # Handle results
    communication_string = ""

    if (len(arrows) == 4):
        for arrow in arrows:
            communication_string += arrow[0]
    else:
        communication_string += "err"


    t.write(communication_string)
    t.write("Closing connection")
    t.close()


