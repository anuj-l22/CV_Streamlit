# -*- coding: utf-8 -*-
"""
App for Domain-based Image Classification Demo
Converted from demo.py (originally generated by Colab)
"""

import os
import random
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
from torchvision import transforms, models
from PIL import Image
import matplotlib.pyplot as plt
import streamlit as st
import numpy as np
import copy
import timm
# Set device
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# ------------------------------------------------------------------------------
# Model and Helper Function Definitions (as in your demo.py)
# ------------------------------------------------------------------------------

def conv3x3(in_planes, out_planes, stride=1):
    return nn.Conv2d(
        in_planes,
        out_planes,
        kernel_size=3,
        stride=stride,
        padding=1,
        bias=True)

def conv_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        nn.init.xavier_uniform_(m.weight, gain=np.sqrt(2))
        nn.init.constant_(m.bias, 0)
    elif classname.find('BatchNorm') != -1:
        nn.init.constant_(m.weight, 1)
        nn.init.constant_(m.bias, 0)

class wide_basic(nn.Module):
    def __init__(self, in_planes, planes, dropout_rate, stride=1):
        super(wide_basic, self).__init__()
        self.bn1 = nn.BatchNorm2d(in_planes)
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=3, padding=1, bias=True)
        self.dropout = nn.Dropout(p=dropout_rate)
        self.bn2 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride, padding=1, bias=True)
        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, planes, kernel_size=1, stride=stride, bias=True), )
    def forward(self, x):
        out = self.dropout(self.conv1(F.relu(self.bn1(x))))
        out = self.conv2(F.relu(self.bn2(out)))
        out += self.shortcut(x)
        return out

class Wide_ResNet(nn.Module):
    """Wide Resnet with the softmax layer chopped off"""
    def __init__(self, input_shape, depth, widen_factor, dropout_rate):
        super(Wide_ResNet, self).__init__()
        self.in_planes = 16
        assert ((depth - 4) % 6 == 0), 'Wide-resnet depth should be 6n+4'
        n = (depth - 4) / 6
        k = widen_factor
        nStages = [16, 16 * k, 32 * k, 64 * k]
        self.conv1 = conv3x3(input_shape[0], nStages[0])
        self.layer1 = self._wide_layer(wide_basic, nStages[1], n, dropout_rate, stride=1)
        self.layer2 = self._wide_layer(wide_basic, nStages[2], n, dropout_rate, stride=2)
        self.layer3 = self._wide_layer(wide_basic, nStages[3], n, dropout_rate, stride=2)
        self.bn1 = nn.BatchNorm2d(nStages[3], momentum=0.9)
        self.n_outputs = nStages[3]
        self.activation = nn.Identity()  # for URM; does not affect other algorithms
    def _wide_layer(self, block, planes, num_blocks, dropout_rate, stride):
        strides = [stride] + [1] * (int(num_blocks) - 1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_planes, planes, dropout_rate, stride))
            self.in_planes = planes
        return nn.Sequential(*layers)
    def forward(self, x):
        out = self.conv1(x)
        out = self.layer1(out)
        out = self.layer2(out)
        out = self.layer3(out)
        out = F.relu(self.bn1(out))
        out = F.avg_pool2d(out, 8)
        out = out[:, :, 0, 0]
        out = self.activation(out)
        return out

class Identity(nn.Module):
    """An identity layer"""
    def __init__(self):
        super(Identity, self).__init__()
    def forward(self, x):
        return x

class MLP(nn.Module):
    """Just an MLP"""
    def __init__(self, n_inputs, n_outputs, hparams):
        super(MLP, self).__init__()
        self.input = nn.Linear(n_inputs, hparams['mlp_width'])
        self.dropout = nn.Dropout(hparams['mlp_dropout'])
        self.hiddens = nn.ModuleList([
            nn.Linear(hparams['mlp_width'], hparams['mlp_width'])
            for _ in range(hparams['mlp_depth'] - 2)])
        self.output = nn.Linear(hparams['mlp_width'], n_outputs)
        self.n_outputs = n_outputs
        self.activation = nn.Identity()
    def forward(self, x):
        x = self.input(x)
        x = self.dropout(x)
        x = F.relu(x)
        for hidden in self.hiddens:
            x = hidden(x)
            x = self.dropout(x)
            x = F.relu(x)
        x = self.output(x)
        x = self.activation(x)
        return x

class DinoV2(nn.Module):
    def __init__(self, input_shape, hparams):
        super(DinoV2, self).__init__()
        self.network = torch.hub.load('facebookresearch/dinov2', 'dinov2_vitb14')
        self.n_outputs = 5 * 768
        nc = input_shape[0]
        if nc != 3:
            raise RuntimeError("Inputs must have 3 channels")
        self.hparams = hparams
        self.dropout = nn.Dropout(hparams['vit_dropout'])
        if hparams["vit_attn_tune"]:
            for n, p in self.network.named_parameters():
                p.requires_grad = ('attn' in n)
    def forward(self, x):
        x = self.network.get_intermediate_layers(x, n=4, return_class_token=True)
        linear_input = torch.cat([
            x[0][1],
            x[1][1],
            x[2][1],
            x[3][1],
            x[3][0].mean(1)
        ], dim=1)
        return self.dropout(linear_input)

class ResNet(nn.Module):
    """ResNet with the softmax chopped off and the batchnorm frozen"""
    def __init__(self, input_shape, hparams):
        super(ResNet, self).__init__()
        if hparams['resnet18']:
            self.network = torchvision.models.resnet18(pretrained=True)
            self.n_outputs = 512
        else:
            self.network = torchvision.models.resnet50(pretrained=True)
            self.n_outputs = 2048
        if hparams['resnet50_augmix']:
            self.network = timm.create_model('resnet50.ram_in1k', pretrained=True)
            self.n_outputs = 2048
        nc = input_shape[0]
        if nc != 3:
            tmp = self.network.conv1.weight.data.clone()
            self.network.conv1 = nn.Conv2d(nc, 64, kernel_size=(7, 7), stride=(2, 2), padding=(3, 3), bias=False)
            for i in range(nc):
                self.network.conv1.weight.data[:, i, :, :] = tmp[:, i % 3, :, :]
        del self.network.fc
        self.network.fc = Identity()
        if hparams["freeze_bn"]:
            self.freeze_bn()
        self.hparams = hparams
        self.dropout = nn.Dropout(hparams['resnet_dropout'])
        self.activation = nn.Identity()
    def forward(self, x):
        return self.activation(self.dropout(self.network(x)))
    def train(self, mode=True):
        super().train(mode)
        if self.hparams["freeze_bn"]:
            self.freeze_bn()
    def freeze_bn(self):
        for m in self.network.modules():
            if isinstance(m, nn.BatchNorm2d):
                m.eval()

class MNIST_CNN(nn.Module):
    """Hand-tuned architecture for MNIST."""
    n_outputs = 128
    def __init__(self, input_shape):
        super(MNIST_CNN, self).__init__()
        self.conv1 = nn.Conv2d(input_shape[0], 64, 3, 1, padding=1)
        self.conv2 = nn.Conv2d(64, 128, 3, stride=2, padding=1)
        self.conv3 = nn.Conv2d(128, 128, 3, 1, padding=1)
        self.conv4 = nn.Conv2d(128, 128, 3, 1, padding=1)
        self.bn0 = nn.GroupNorm(8, 64)
        self.bn1 = nn.GroupNorm(8, 128)
        self.bn2 = nn.GroupNorm(8, 128)
        self.bn3 = nn.GroupNorm(8, 128)
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.activation = nn.Identity()
    def forward(self, x):
        x = self.conv1(x)
        x = F.relu(x)
        x = self.bn0(x)
        x = self.conv2(x)
        x = F.relu(x)
        x = self.bn1(x)
        x = self.conv3(x)
        x = F.relu(x)
        x = self.bn2(x)
        x = self.conv4(x)
        x = F.relu(x)
        x = self.bn3(x)
        x = self.avgpool(x)
        x = x.view(len(x), -1)
        return self.activation(x)

class ContextNet(nn.Module):
    def __init__(self, input_shape):
        super(ContextNet, self).__init__()
        padding = (5 - 1) // 2
        self.context_net = nn.Sequential(
            nn.Conv2d(input_shape[0], 64, 5, padding=padding),
            nn.BatchNorm2d(64),
            nn.ReLU(),
            nn.Conv2d(64, 64, 5, padding=padding),
            nn.BatchNorm2d(64),
            nn.ReLU(),
            nn.Conv2d(64, 1, 5, padding=padding),
        )
    def forward(self, x):
        return self.context_net(x)

def Featurizer(input_shape, hparams):
    if len(input_shape) == 1:
        return MLP(input_shape[0], hparams["mlp_width"], hparams)
    elif input_shape[1:3] == (28, 28):
        return MNIST_CNN(input_shape)
    elif input_shape[1:3] == (32, 32):
        return Wide_ResNet(input_shape, 16, 2, 0.)
    elif input_shape[1:3] == (224, 224):
        if hparams["vit"]:
            if hparams["dinov2"]:
                return DinoV2(input_shape, hparams)
            else:
                raise NotImplementedError
        return ResNet(input_shape, hparams)
    else:
        raise NotImplementedError

def Classifier(in_features, out_features, is_nonlinear=False):
    if is_nonlinear:
        return nn.Sequential(
            nn.Linear(in_features, in_features // 2),
            nn.ReLU(),
            nn.Linear(in_features // 2, in_features // 4),
            nn.ReLU(),
            nn.Linear(in_features // 4, out_features))
    else:
        return nn.Linear(in_features, out_features)

class WholeFish(nn.Module):
    def __init__(self, input_shape, num_classes, hparams, weights=None):
        super(WholeFish, self).__init__()
        featurizer = Featurizer(input_shape, hparams)
        classifier = Classifier(featurizer.n_outputs, num_classes, hparams['nonlinear_classifier'])
        self.net = nn.Sequential(featurizer, classifier)
        if weights is not None:
            self.load_state_dict(copy.deepcopy(weights))
    def reset_weights(self, weights):
        self.load_state_dict(copy.deepcopy(weights))
    def forward(self, x):
        return self.net(x)

# ------------------------------------------------------------------------------
# Define Hyperparameters, Input Shape, and Transforms
# ------------------------------------------------------------------------------

hparams = {
    'data_augmentation': True, 'resnet18': False, 'resnet50_augmix': True,
    'dinov2': False, 'vit': False, 'vit_attn_tune': False, 'freeze_bn': False,
    'lars': False, 'linear_steps': 500, 'resnet_dropout': 0.0, 'vit_dropout': 0.0,
    'class_balanced': False, 'nonlinear_classifier': False, 'lr': 5e-05,
    'weight_decay': 0.0, 'batch_size': 32
}

input_shape = (3, 224, 224)
class_names = ["dog", "elephant", "guitar", "horse", "house", "person", "tree"]
num_classes = len(class_names)
transform = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                         std=[0.229, 0.224, 0.225])
])

# ------------------------------------------------------------------------------
# Helper Function: Sample a Random Image from a Domain Folder
# ------------------------------------------------------------------------------

def sample_random_image(domain_dir):
    valid_extensions = ['.jpg', '.jpeg', '.png']
    image_paths = []
    for root, _, files in os.walk(domain_dir):
        for file in files:
            if any(file.lower().endswith(ext) for ext in valid_extensions):
                image_paths.append(os.path.join(root, file))
    if not image_paths:
        raise RuntimeError(f"No images found in {domain_dir}. Check your dataset structure.")
    chosen_path = random.choice(image_paths)
    ground_truth = os.path.basename(os.path.dirname(chosen_path))
    image = Image.open(chosen_path).convert("RGB")
    return image, ground_truth, chosen_path

# ------------------------------------------------------------------------------
# Main Demo Inference Code inside a function to trigger on button click.
# ------------------------------------------------------------------------------

# Modify run_demo to accept the selected algorithm (dropdown value)
def run_demo(selected_algo):
    # Adjusting directory structure
    data_dir = "data"
    if not os.path.exists(data_dir):
        st.error(f"Data directory '{data_dir}' not found.")
        return

    sub_dirs = [name for name in os.listdir(data_dir) if os.path.isdir(os.path.join(data_dir, name))]
    if len(sub_dirs) == 1:
        dataset_dir = os.path.join(data_dir, sub_dirs[0])
        domain_names = sorted([name for name in os.listdir(dataset_dir) if os.path.isdir(os.path.join(dataset_dir, name))])
        data_root = dataset_dir
    else:
        domain_names = sorted(sub_dirs)
        data_root = data_dir

    # Display discovered domain folders
    if domain_names:
        st.markdown("**Discovered Domain Folders:**")
        for idx, dname in enumerate(domain_names):
            st.markdown(f"- **{idx}**: `{dname}`")
    else:
        st.warning("No domain folders found in the data directory.")
        return

    domain_results = []

    # For each domain, use the selected algorithm to load its model checkpoint.
    for i, domain_name in enumerate(domain_names):
        domain_data_path = os.path.join(data_root, domain_name)
        # Modified model_dir path: use the selected algorithm from the dropdown.
        model_dir = os.path.join("Deployment", "PACS", selected_algo, str(i))
        model_path = os.path.join(model_dir, "model.pkl")

        # If checkpoint is missing, issue a warning and skip.
        if not os.path.exists(model_path):
            st.warning(f"Model checkpoint for domain `{domain_name}` not found at `{model_path}`.")
            continue

        # Build the WholeFish model and load the checkpoint.
        model = WholeFish(input_shape, num_classes, hparams)
        checkpoint = torch.load(model_path, map_location=device)

        new_state_dict = {}
        for k, v in checkpoint.items():
            if k.startswith("featurizer"):
                new_state_dict[k.replace("featurizer", "net.0")] = v
            elif k.startswith("classifier"):
                new_state_dict[k.replace("classifier", "net.1")] = v
            else:
                new_state_dict[k] = v

        model.load_state_dict(new_state_dict, strict=False)
        model.to(device)
        model.eval()

        # Sample a random image from the domain folder.
        try:
            image, ground_truth, img_path = sample_random_image(domain_data_path)
        except Exception as err:
            st.error(str(err))
            continue

        # Preprocess the image.
        input_tensor = transform(image).unsqueeze(0).to(device)

        # Run inference.
        with torch.no_grad():
            output = model(input_tensor)
            probabilities = F.softmax(output, dim=1)
            pred_index = probabilities.argmax(dim=1).item()
            predicted_label = class_names[pred_index]
            confidence = probabilities[0, pred_index].item()

        domain_results.append({
             "domain": domain_name,
             "image": image,
             "ground_truth": ground_truth,
             "predicted_label": predicted_label,
             "confidence": confidence
        })

    if not domain_results:
        st.info("No valid domain results to display.")
    else:
        st.subheader("Inference Results")
        # Display results 4 per row using st.columns.
        for idx in range(0, len(domain_results), 4):
            row_results = domain_results[idx : idx + 4]
            cols = st.columns(len(row_results))
            for col, result in zip(cols, row_results):
                caption = (
                    f"**Domain:** `{result['domain']}`\n\n"
                    f"**GT:** `{result['ground_truth']}`\n\n"
                    f"**Prediction:** `{result['predicted_label']}`"
                    f" ({result['confidence']*100:.1f}% confidence)"
                )
                col.image(result["image"], caption=caption, use_container_width=True)

# ------------------------------------------------------------------------------
# Streamlit App Interface
# ------------------------------------------------------------------------------

st.title("Domain-Based Image Classification Demo")
st.write("Select an algorithm from the dropdown, then click **Start Demo** to run inference on random images from each domain.")

# --- NEW: Dropdown for Algorithm Selection ---
selected_algo = st.selectbox("Select Algorithm", ["EQRM", "ERM", "ERMPlusPlus", "HOGPACS", "IRM", "URM"])

if st.button("Start Demo"):
    with st.spinner("Running inference, please wait..."):
        run_demo(selected_algo)
