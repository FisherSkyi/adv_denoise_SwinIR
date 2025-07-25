# Copyright 2021 Jingyun Liang
# Modifications copyright 2025 Letian Yu
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import argparse
import cv2
import glob # path name pattern
import numpy as np
from collections import OrderedDict

import torch
import time
import requests
import matplotlib.pyplot as plt

import addshapeall
from models.network_swinir import SwinIR as net
from utils import util_calculate_psnr_ssim as util


def main():
    global img_gt
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', type=str, default='color_dn', help='classical_sr, lightweight_sr, real_sr, '
                                                                     'gray_dn, color_dn, jpeg_car, color_jpeg_car')
    parser.add_argument('--noise_type', type=str, default='gaussian', help='gaussian, graffiti,')
    parser.add_argument('--scale', type=int, default=1, help='scale factor: 1, 2, 3, 4, 8') # 1 for dn and jpeg car
    parser.add_argument('--noise', type=int, default=15, help='noise level: 15, 25, 50')
    parser.add_argument('--jpeg', type=int, default=40, help='scale factor: 10, 20, 30, 40')
    parser.add_argument('--training_patch_size', type=int, default=32, help='patch size used in training SwinIR. '
                                       'Just used to differentiate two different settings in Table 2 of the paper. '
                                       'Images are NOT tested patch by patch.')
    parser.add_argument('--large_model', action='store_true', help='use large model, only provided for real image sr')
    parser.add_argument('--model_path', type=str,
                        default='model_zoo/swinir/005_color_dn_noise15_typegaussian.pth',)
    parser.add_argument('--folder_lq', type=str, default=None, help='input low-quality test image folder')
    parser.add_argument('--folder_gt', type=str, default=None, help='input ground-truth test image folder')
    parser.add_argument('--tile', type=int, default=None, help='Tile size, None for no tile during testing (testing as a whole)')
    parser.add_argument('--tile_overlap', type=int, default=32, help='Overlapping of different tiles')
    args = parser.parse_args()

    device = torch.device('mps' if torch.mps.is_available() else 'cpu')
    # set up model
    if os.path.exists(args.model_path):
        print(f'loading model from {args.model_path}')
    else:
        os.makedirs(os.path.dirname(args.model_path), exist_ok=True)
        url = 'https://github.com/JingyunLiang/SwinIR/releases/download/v0.0/{}'.format(os.path.basename(args.model_path))
        r = requests.get(url, allow_redirects=True)
        print(f'downloading model {args.model_path}')
        open(args.model_path, 'wb').write(r.content)

    model = define_model(args)
    model.eval()
    model = model.to(device)

    # setup folder and path
    folder, save_dir, border, window_size = setup(args)
    os.makedirs(save_dir, exist_ok=True)
    test_results = OrderedDict()
    test_results['psnr'] = []
    test_results['ssim'] = []
    test_results['psnr_y'] = []
    test_results['ssim_y'] = []
    test_results['psnrb'] = []
    test_results['psnrb_y'] = []
    psnr, ssim, psnr_y, ssim_y, psnrb, psnrb_y = 0, 0, 0, 0, 0, 0

    np.random.seed(seed=0)
    for idx, path in enumerate(sorted(glob.glob(os.path.join(folder, '*')))):
        # this match all files in the folder, including subfolders
        # read image
        # add noise in the form of a pair of images (lq, gt)
        imgname, img_lq, img_gt = get_image_pair(args, path)  # image to HWC(Height-Weight-Channel)-BGR, float32
        img_lq = np.transpose(img_lq if img_lq.shape[2] == 1 else img_lq[:, :, [2, 1, 0]], (2, 0, 1))  # HCW-BGR to CHW-RGB
        img_lq = torch.from_numpy(img_lq).float().unsqueeze(0).to(device)  # CHW-RGB to NCHW-RGB(Batch Size, Channels, Height, Width)

        # inference
        with torch.no_grad():
            # pad input image to be a multiple of window_size
            _, _, h_old, w_old = img_lq.size()
            h_pad = (h_old // window_size + 1) * window_size - h_old
            w_pad = (w_old // window_size + 1) * window_size - w_old
            img_lq = torch.cat([img_lq, torch.flip(img_lq, [2])], 2)[:, :, :h_old + h_pad, :]
            img_lq = torch.cat([img_lq, torch.flip(img_lq, [3])], 3)[:, :, :, :w_old + w_pad]
            output = test(img_lq, model, args, window_size)
            output = output[..., :h_old * args.scale, :w_old * args.scale]

        # save image
        output = output.data.squeeze().float().cpu().clamp_(0, 1).numpy()
        if output.ndim == 3:
            output = np.transpose(output[[2, 1, 0], :, :], (1, 2, 0))  # CHW-RGB to HCW-BGR
        output = (output * 255.0).round().astype(np.uint8)  # float32 to uint8

        if img_gt is not None:
            # print(img_gt.s)
            clean_img = img_gt[:,:,::-1]
            noisy_img = img_lq.cpu().numpy().squeeze()
            if noisy_img.ndim == 3:
                noisy_img = np.transpose(noisy_img, (1, 2, 0))  # CHW to HWC
                # noisy_img_for_plot = np.clip(noisy_img, 0, 1)  # float32 to uint8
            denoised_img = output / 255.0 if output.max() > 1 else output  # Ensure float [0,1]
            denoised_img = denoised_img[:, :, ::-1]  # BGR to RGB for plotting

            fig, axs = plt.subplots(1, 3, figsize=(12, 4))
            axs[0].imshow(clean_img)
            axs[0].set_title('Clean Image')
            axs[1].imshow(noisy_img)
            axs[1].set_title('Noisy Image')
            axs[2].imshow(denoised_img)
            axs[2].set_title('Denoised Image')
            for ax in axs:
                ax.axis('off')
            plt.tight_layout()
            plt.show()
        cv2.imwrite(f'{save_dir}/{imgname}_SwinIR.png', output)

        # evaluate psnr/ssim/psnr_b
        if img_gt is not None:
            img_gt = (img_gt * 255.0).round().astype(np.uint8)  # float32 to uint8
            img_gt = img_gt[:h_old * args.scale, :w_old * args.scale, ...]  # crop gt
            img_gt = np.squeeze(img_gt)


            psnr = util.calculate_psnr(output, img_gt, crop_border=border)
            ssim = util.calculate_ssim(output, img_gt, crop_border=border)
            test_results['psnr'].append(psnr)
            test_results['ssim'].append(ssim)
            if img_gt.ndim == 3:  # RGB image
                psnr_y = util.calculate_psnr(output, img_gt, crop_border=border, test_y_channel=True)
                ssim_y = util.calculate_ssim(output, img_gt, crop_border=border, test_y_channel=True)
                test_results['psnr_y'].append(psnr_y)
                test_results['ssim_y'].append(ssim_y)
            if args.task in ['jpeg_car', 'color_jpeg_car']:
                psnrb = util.calculate_psnrb(output, img_gt, crop_border=border, test_y_channel=False)
                test_results['psnrb'].append(psnrb)
                if args.task in ['color_jpeg_car']:
                    psnrb_y = util.calculate_psnrb(output, img_gt, crop_border=border, test_y_channel=True)
                    test_results['psnrb_y'].append(psnrb_y)
            print('Testing {:d} {:20s} - PSNR: {:.2f} dB; SSIM: {:.4f}; PSNRB: {:.2f} dB;'
                  'PSNR_Y: {:.2f} dB; SSIM_Y: {:.4f}; PSNRB_Y: {:.2f} dB.'.
                  format(idx, imgname, psnr, ssim, psnrb, psnr_y, ssim_y, psnrb_y))
        else:
            print('Testing {:d} {:20s}'.format(idx, imgname))

    # summarize psnr/ssim
    if img_gt is not None:
        ave_psnr = sum(test_results['psnr']) / len(test_results['psnr'])
        ave_ssim = sum(test_results['ssim']) / len(test_results['ssim'])
        print('\n{} \n-- Average PSNR/SSIM(RGB): {:.2f} dB; {:.4f}'.format(save_dir, ave_psnr, ave_ssim))
        if img_gt.ndim == 3:
            ave_psnr_y = sum(test_results['psnr_y']) / len(test_results['psnr_y'])
            ave_ssim_y = sum(test_results['ssim_y']) / len(test_results['ssim_y'])
            print('-- Average PSNR_Y/SSIM_Y: {:.2f} dB; {:.4f}'.format(ave_psnr_y, ave_ssim_y))
        if args.task in ['jpeg_car', 'color_jpeg_car']:
            ave_psnrb = sum(test_results['psnrb']) / len(test_results['psnrb'])
            print('-- Average PSNRB: {:.2f} dB'.format(ave_psnrb))
            if args.task in ['color_jpeg_car']:
                ave_psnrb_y = sum(test_results['psnrb_y']) / len(test_results['psnrb_y'])
                print('-- Average PSNRB_Y: {:.2f} dB'.format(ave_psnrb_y))


def define_model(args):
    # 004 grayscale image denoising
    # if args.task == 'gray_dn':
    #     model = net(upscale=1, in_chans=1, img_size=128, window_size=8,
    #                 img_range=1., depths=[6, 6, 6, 6, 6, 6], embed_dim=180, num_heads=[6, 6, 6, 6, 6, 6],
    #                 mlp_ratio=2, upsampler='', resi_connection='1conv')
    #     param_key_g = 'params'

    # 005 color image denoising
    # elif args.task == 'color_dn':
    model = net(upscale=1, in_chans=3, img_size=128, window_size=8,
                img_range=1., depths=[6, 6, 6, 6, 6, 6], embed_dim=180, num_heads=[6, 6, 6, 6, 6, 6],
                mlp_ratio=2, upsampler='', resi_connection='1conv')
    param_key_g = 'params'

    pretrained_model = torch.load(args.model_path, map_location='mps')
    # load pretrained model
    model.load_state_dict(pretrained_model[param_key_g] if param_key_g in pretrained_model.keys() else pretrained_model, strict=True)

    return model


def setup(args):
    timestamp = time.strftime('%Y%m%d-%H%M%S')
    # 001 classical image sr/ 002 lightweight image sr

    # 004 grayscale image denoising/ 005 color image denoising
    # if args.task in ['gray_dn', 'color_dn']:
    save_dir = f'results/swinir_{args.task}_noise{args.noise}_type{args.noise_type}_{timestamp}'
    folder = args.folder_gt
    border = 0
    window_size = 8

    return folder, save_dir, border, window_size


def get_image_pair(args, path):
    (imgname, imgext) = os.path.splitext(os.path.basename(path))


    # 004 grayscale image denoising (load gt image and generate lq image on-the-fly)
    # if args.task in ['gray_dn']:
    #     img_gt = cv2.imread(path, cv2.IMREAD_GRAYSCALE).astype(np.float32) / 255.
    #     np.random.seed(seed=0)
    #     img_lq = img_gt + np.random.normal(0, args.noise / 255., img_gt.shape)
    #     img_gt = np.expand_dims(img_gt, axis=2)
    #     img_lq = np.expand_dims(img_lq, axis=2)

    # 005 color image denoising (load gt image and generate lq image on-the-fly)
    # elif args.task in ['color_dn']:
    img_gt = cv2.imread(path, cv2.IMREAD_COLOR).astype(np.float32) / 255.
    if args.noise_type == 'gaussian':
        img_lq = img_gt + np.random.normal(0, args.noise / 255., img_gt.shape)
    elif args.noise_type == 'graffiti':
        img_lq = np.array(addshapeall.add_random_shapes(img_gt, 0.3)).astype(np.float32) / 255.

    return imgname, img_lq, img_gt


def test(img_lq, model, args, window_size):
    if args.tile is None:
        # test the image as a whole
        # the model is a pretrained SwinIR model that maps img_lq (noisy image) → output (denoised image)
        output = model(img_lq)
    else:
        # test the image tile by tile
        b, c, h, w = img_lq.size()
        tile = min(args.tile, h, w)
        assert tile % window_size == 0, "tile size should be a multiple of window_size"
        tile_overlap = args.tile_overlap
        sf = args.scale

        stride = tile - tile_overlap
        h_idx_list = list(range(0, h-tile, stride)) + [h-tile]
        w_idx_list = list(range(0, w-tile, stride)) + [w-tile]
        E = torch.zeros(b, c, h*sf, w*sf).type_as(img_lq)
        W = torch.zeros_like(E)

        for h_idx in h_idx_list:
            for w_idx in w_idx_list:
                in_patch = img_lq[..., h_idx:h_idx+tile, w_idx:w_idx+tile]
                out_patch = model(in_patch)
                out_patch_mask = torch.ones_like(out_patch)

                E[..., h_idx*sf:(h_idx+tile)*sf, w_idx*sf:(w_idx+tile)*sf].add_(out_patch)
                W[..., h_idx*sf:(h_idx+tile)*sf, w_idx*sf:(w_idx+tile)*sf].add_(out_patch_mask)
        output = E.div_(W)

    return output

if __name__ == '__main__':
    main()
