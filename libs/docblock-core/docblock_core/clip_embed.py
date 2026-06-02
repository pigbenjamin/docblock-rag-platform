#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from typing import List
from PIL import Image

def clip_image_embed(image_path: str, device: str = "cuda") -> List[float]:
    """
    pip install transformers pillow torch
    """
    import torch
    from transformers import CLIPProcessor, CLIPModel

    model_name = os.getenv("CLIP_MODEL", "openai/clip-vit-large-patch14")
    model = CLIPModel.from_pretrained(model_name).to(device)
    proc = CLIPProcessor.from_pretrained(model_name)

    img = Image.open(image_path).convert("RGB")
    inputs = proc(images=img, return_tensors="pt").to(device)
    with torch.no_grad():
        feats = model.get_image_features(**inputs)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats[0].detach().cpu().tolist()

import torch
from transformers import CLIPProcessor, CLIPModel

class ClipEmbedder:

    def __init__(self, device="cuda"):
        self.model_name = os.getenv(
            "CLIP_MODEL",
            "openai/clip-vit-large-patch14"
        )

        self.model = CLIPModel.from_pretrained(self.model_name).to(device)
        self.proc = CLIPProcessor.from_pretrained(self.model_name)
        self.device = device

    def embed_image(self, image_path):
        img = Image.open(image_path).convert("RGB")

        inputs = self.proc(images=img, return_tensors="pt").to(self.device)

        with torch.no_grad():
            feats = self.model.get_image_features(**inputs)
            feats = feats / feats.norm(dim=-1, keepdim=True)

        return feats[0].cpu().tolist()


def clip_text_embed(text: str, device: str = "cuda") -> List[float]:
    """
    查 image 時，query 要用同一個 CLIP text encoder
    """
    import torch
    from transformers import CLIPProcessor, CLIPModel

    model_name = os.getenv("CLIP_MODEL", "openai/clip-vit-large-patch14")
    model = CLIPModel.from_pretrained(model_name).to(device)
    proc = CLIPProcessor.from_pretrained(model_name)

    inputs = proc(text=[text], return_tensors="pt", padding=True).to(device)
    with torch.no_grad():
        feats = model.get_text_features(**inputs)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats[0].detach().cpu().tolist()
