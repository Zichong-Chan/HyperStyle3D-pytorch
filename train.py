import os
import argparse
from tqdm import tqdm
import torch
import torch.optim as optim
import torchvision
from loss import CLIPLoss, IDLoss
from StyleSDF.model import Generator
from StyleSDF.options import BaseOptions
from StyleSDF.utils import generate_camera_params
from model import HyperModule, TextEncoder
from util.matting.matting import Matting
import warnings
warnings.filterwarnings('ignore', category=UserWarning)


def generate(opt, src_generator, tgt_generator, device, mean_latent, weights_delta=None):
    """generate images from different views by src_generator and tgt_generator
    return low&high resolution images generated by two generators
    """
    src_generator.eval()
    tgt_generator.eval()
    # locations = torch.tensor([[0, 0],
    #                           [-1.5 * opt.camera.azim, 0],
    #                           [-1 * opt.camera.azim, 0],
    #                           [-0.5 * opt.camera.azim, 0],
    #                           [0.5 * opt.camera.azim, 0],
    #                           [1 * opt.camera.azim, 0],
    #                           [1.5 * opt.camera.azim, 0],
    #                           [0, -1.5 * opt.camera.elev],
    #                           [0, -1 * opt.camera.elev],
    #                           [0, -0.5 * opt.camera.elev],
    #                           [0, 0.5 * opt.camera.elev],
    #                           [0, 1 * opt.camera.elev],
    #                           [0, 1.5 * opt.camera.elev]], device=device)

    azim_rand, elev_rand = torch.rand(1)*1.5, torch.rand(1)*1.5
    locations = torch.tensor([[0, 0],
                              [-azim_rand * opt.camera.azim, 0],
                              [azim_rand * opt.camera.azim, 0],
                              [0, -elev_rand * opt.camera.elev],
                              [0, elev_rand * opt.camera.elev]
                              ], device=device)

    fov = opt.camera.fov * torch.ones((locations.shape[0], 1), device=device)
    num_viewdirs = locations.shape[0]

    chunk = 4
    sample_z = torch.randn(1, opt.style_dim, device=device).repeat(num_viewdirs, 1)
    sample_cam_extrinsics, sample_focals, sample_near, sample_far, sample_locations = \
        generate_camera_params(opt.renderer_output_size, device, batch=num_viewdirs,
                               locations=locations,  # input_fov=fov,
                               uniform=opt.camera.uniform, azim_range=opt.camera.azim,
                               elev_range=opt.camera.elev, fov_ang=fov,
                               dist_radius=opt.camera.dist_radius)
    src_rgb_images = torch.Tensor(0, 3, opt.size, opt.size)  # 1024
    src_rgb_images_thumbs = torch.Tensor(0, 3, opt.renderer_output_size, opt.renderer_output_size)  # 64
    tgt_rgb_images = torch.Tensor(0, 3, opt.size, opt.size)  # 1024
    tgt_rgb_images_thumbs = torch.Tensor(0, 3, opt.renderer_output_size, opt.renderer_output_size)  # 64
    for j in range(0, num_viewdirs, chunk):
        # generate image by target generator
        out = tgt_generator([sample_z[j:j + chunk]],
                            sample_cam_extrinsics[j:j + chunk],
                            sample_focals[j:j + chunk],
                            sample_near[j:j + chunk],
                            sample_far[j:j + chunk],
                            truncation=opt.truncation_ratio,
                            truncation_latent=mean_latent,
                            weights_delta=weights_delta)
        tgt_rgb_images = torch.cat([tgt_rgb_images, out[0].cpu()], 0)
        tgt_rgb_images_thumbs = torch.cat([tgt_rgb_images_thumbs, out[1].cpu()], 0)

        # generate image by source generator
        with torch.no_grad():
            out = src_generator([sample_z[j:j + chunk]],
                                sample_cam_extrinsics[j:j + chunk],
                                sample_focals[j:j + chunk],
                                sample_near[j:j + chunk],
                                sample_far[j:j + chunk],
                                truncation=opt.truncation_ratio,
                                truncation_latent=mean_latent)
            src_rgb_images = torch.cat([src_rgb_images, out[0].cpu()], 0)
            src_rgb_images_thumbs = torch.cat([src_rgb_images_thumbs, out[1].cpu()], 0)

    return src_rgb_images, src_rgb_images_thumbs, tgt_rgb_images, tgt_rgb_images_thumbs, locations.shape[0]


def save_models(network, save_dir):
    if not os.path.exists(save_dir):
        os.mkdir(save_dir)
    torch.save(network.coarse.state_dict(), os.path.join(save_dir, f'hyper_coarse.pt'))
    torch.save(network.medium.state_dict(), os.path.join(save_dir, 'hyper_medium.pt'))
    torch.save(network.fine.state_dict(), os.path.join(save_dir, 'hyper_fine.pt'))


def train(args, opt, src_generator: Generator, tgt_generator: Generator,
          hyper_network: HyperModule, optimizer, mean_latent, start_iter=0):

    # init region loss
    matte = Matting('util/pretrained_models/MODEL.pth', device=device)

    criterion = {
        'clip': CLIPLoss(device=device),
        'id': IDLoss().to(device).eval(),
        'region': matte.compute_region_loss
    }

    pbar = tqdm(range(args.epoch), initial=start_iter, dynamic_ncols=True, smoothing=0.001)
    for ii in pbar:
        idx = ii + start_iter

        src_txt = {'shape_txt': 'face',
                   'attribute_txt': 'age',
                   'style_txt': 'photograph of human face'}
        tgt_txt = {'shape_txt': 'face',
                   'attribute_txt': 'young age',
                   'style_txt': 'photograph of Elf human head '}

        src_concat = ', '.join([i for i in src_txt.values()])
        tgt_concat = ', '.join([i for i in tgt_txt.values()])
        delta = hyper_network(src_txt, tgt_txt)


        ppl = hyper_network.parameters_per_layer
        weights_delta = []
        prefix = 0
        for i in range(8):
            weights_delta.append(delta[prefix:prefix+ppl[i]])
            prefix += ppl[i]
        weights_delta.append(delta[prefix:])

        src_hr_imgs, src_lr_imgs, tgt_hr_imgs, tgt_lr_imgs, n_samples = generate(opt.inference, src_generator,
                                                                                 tgt_generator,
                                                                                 device, mean_latent,
                                                                                 weights_delta=weights_delta)

        # losses
        clip_loss = criterion['clip'](tgt_hr_imgs, src_hr_imgs, tgt_concat, src_concat, n_samples) * args.w_clip_loss
        id_loss = criterion['id'](tgt_hr_imgs, src_hr_imgs, n_samples) * args.w_id_loss
        region_loss = 0.
        if args.w_region_loss > 0.:
            region_loss = criterion['region'](tgt_hr_imgs, src_hr_imgs) * args.w_region_loss


        loss_total = clip_loss + id_loss + region_loss
        optimizer.zero_grad()
        loss_total.backward()
        optimizer.step()

        clip_loss_val = clip_loss.mean().item()
        id_loss_val = id_loss.mean().item()
        region_loss_val = region_loss.mean().item() if args.w_region_loss > 0. else 0.

        pbar.set_description(f'iter {idx} | clip:{clip_loss_val:.5f}, id:{id_loss_val:.5f}, region:{region_loss_val:.5f}')

        # save model per 50 iterations
        if idx % 50 == 0:
            save_dir = f'./output/iter{str(idx).zfill(4)}'
            save_models(hyper_network, save_dir)
            print(f'Successfully saved checkpoint and synthesis images for iteration {idx}.')
        with torch.no_grad():
            src_samples = torch.Tensor(0, 3, args.output_size, args.output_size)
            tgt_samples = torch.Tensor(0, 3, args.output_size, args.output_size)

            src_hr_imgs, src_lr_imgs, tgt_hr_imgs, tgt_lr_imgs, n_samples = generate(opt.inference, src_generator,
                                                                                     tgt_generator,
                                                                                     device, mean_latent,
                                                                                     weights_delta=weights_delta)
            src_samples = torch.cat([src_samples, src_hr_imgs.cpu()[:6:2]], 0)
            tgt_samples = torch.cat([tgt_samples, tgt_hr_imgs.cpu()[:6:2]], 0)
            samples = torch.cat([src_samples, tgt_samples], 0)
            torchvision.utils.save_image(samples, os.path.join('./output', f'sample{idx}.png'),
                                         nrow=3,
                                         normalize=True,
                                         value_range=(-1, 1),

        # save temp model each iteration
        save_dir = f'./output/temp'
        save_models(hyper_network, save_dir)
        with open(os.path.join(save_dir, 'start'), 'w') as file:
            file.write(f'{idx}')

    save_dir = f'./output/final'
    save_models(hyper_network, save_dir)
    print('Successfully saved final model.')
    print('Done!')



if __name__ == '__main__':
    print('initializing...')
    device = "cpu"

    parser = argparse.ArgumentParser()
    parser.add_argument('--w_clip_loss', type=float, default=.5)
    parser.add_argument('--w_id_loss', type=float, default=1.)
    parser.add_argument('--w_region_loss', type=float, default=1.)
    parser.add_argument('--epoch', type=int, default=60)       # 2000
    parser.add_argument('--lr', type=float, default=0.0002)
    parser.add_argument('--group', type=str, default='333', help='division of coarse, medium and fine. default [3,3,3]')
    parser.add_argument('--expname', type=str, default='ffhq1024x1024')
    parser.add_argument('--output_size', type=int, default=1024)
    parser.add_argument('--continuous', type=bool, default=True, help='continuous training starting from temp')
    parser.add_argument('--temp_model_dir', type=str, default='./output/temp',
                        help='directory of temp models for continuous training')
    arguments = parser.parse_args()

    opt = BaseOptions().parse()
    opt.training.camera = opt.camera
    opt.training.size = opt.model.size
    opt.training.renderer_output_size = opt.model.renderer_spatial_output_dim
    opt.training.style_dim = opt.model.style_dim
    opt.model.freeze_renderer = False
    opt.experiment.expname = arguments.expname
    opt.model.size = 1024
    opt.inference.camera = opt.camera
    opt.inference.size = opt.model.size
    opt.inference.renderer_output_size = opt.model.renderer_spatial_output_dim
    opt.inference.style_dim = opt.model.style_dim
    opt.inference.project_noise = opt.model.project_noise
    opt.inference.return_xyz = opt.rendering.return_xyz

    checkpoints_dir = 'StyleSDF/full_models'
    checkpoints_path = os.path.join(checkpoints_dir, opt.experiment.expname + '.pt')

    print('loading pre-trained models...')
    checkpoint = torch.load(checkpoints_path)
    src_g = Generator(opt.model, opt.rendering).to(device)
    pretrained_weights_dict = checkpoint['g_ema']
    model_dict = src_g.state_dict()
    for k, v in pretrained_weights_dict.items():
        if v.size() == model_dict[k].size():
            model_dict[k] = v
    src_g.load_state_dict(model_dict)

    tgt_g = Generator(opt.model, opt.rendering)
    tgt_g = tgt_g.to(device)
    pretrained_weights_dict = checkpoint['g_ema']
    model_dict = tgt_g.state_dict()
    for k, v in pretrained_weights_dict.items():
        if v.size() == model_dict[k].size():
            model_dict[k] = v
    tgt_g.load_state_dict(model_dict)
    tgt_g = tgt_g.train()

    # get the mean latent vector for g_ema
    if opt.inference.truncation_ratio < 1:
        with torch.no_grad():
            mean_latent = src_g.mean_latent(opt.inference.truncation_mean, device)

    print('initializing text encoder and hyper-network...')
    text_encoder = TextEncoder(device=device).to(device)
    hyper = HyperModule(encoder=text_encoder, g=tgt_g, in_feat=512, group=list(map(int, list(arguments.group))))
    hyper = hyper.to(device)

    start = 0
    # load parameters from saved temp models
    if arguments.continuous and os.path.exists(arguments.temp_model_dir) and os.listdir(arguments.temp_model_dir):
        print('loading parameters from saved temp models...')
        modules = [hyper.coarse, hyper.medium, hyper.fine]
        seq = ['coarse', 'medium', 'fine']
        for index, module in enumerate(modules):
            print(f'---copying parameters of {seq[index]} network...')
            checkpoint = torch.load(os.path.join(arguments.temp_model_dir, 'hyper_'+seq[index]+'.pt'))
            model_dict = module.state_dict()
            for k, v in checkpoint.items():
                if v.size() == model_dict[k].size():
                    model_dict[k] = v
            module.load_state_dict(model_dict)
        with open(os.path.join(arguments.temp_model_dir, 'start'), 'r') as f:
            start = int(f.readline())+1

    optimizer = optim.Adam([{'params': hyper.coarse.parameters()},
                            {'params': hyper.medium.parameters()},
                            {'params': hyper.fine.parameters()}],
                           lr=arguments.lr)
    print('start training...')
    train(arguments, opt, src_g, tgt_g, hyper, optimizer, mean_latent, start_iter=start)
