import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from ..datasets import *
from ..utils import *
from scipy.io import wavfile
import numpy as np
import timeit
from torch.autograd import Variable
from tensorboardX import SummaryWriter
from .generator import Generator
from .discriminator import Discriminator
from .core import *
import json
import os


class SEGAN(Model):

    def __init__(self, opts, name='SEGAN'):
        super(SEGAN, self).__init__(name)
        self.opts = opts
        self.preemph = opts.preemph
        self.save_path = opts.save_path
        self.do_cuda = opts.cuda
        self.g_dropout = opts.g_dropout
        self.z_dim = opts.z_dim
        self.g_enc_fmaps = opts.g_enc_fmaps
        self.d_iter = opts.d_iter
        self.g_enc_prelus = [nn.PReLU(fmaps) for fmaps in self.g_enc_fmaps]
        self.g_dec_prelus = [nn.PReLU(fmaps) for fmaps in \
                             self.g_enc_fmaps[::-1][1:] + [1]]
        # Build G and D
        self.G = Generator(1, self.g_enc_fmaps, opts.kwidth,
                           self.g_enc_prelus,
                           opts.g_bnorm, opts.g_dropout, 
                           pooling=2, z_dim=opts.z_dim,
                           z_all=opts.z_all,
                           cuda=opts.cuda,
                           skip=opts.skip,
                           dec_activations=self.g_dec_prelus)
        print('Generator: ', self.G)

        self.D = Discriminator(2, self.g_enc_fmaps, opts.kwidth,
                               nn.LeakyReLU(0.3), bnorm=opts.d_bnorm,
                               pooling=2, SND=opts.SND,
                               rnn_pool=opts.D_rnn_pool)
        print('Discriminator: ', self.D)
        # create writer
        self.writer = SummaryWriter(os.path.join(opts.save_path, 'train'))

    def train(self, opts, dloader, criterion, l1_init, l1_dec_step,
              l1_dec_epoch, log_freq, va_dloader=None):

        """ Train the SEGAN """
        Gopt = getattr(optim, opts.g_opt)(self.G.parameters(), lr=opts.g_lr,
                                          betas=(opts.beta_1, 0.99))
        Dopt = getattr(optim, opts.d_opt)(self.D.parameters(), lr=opts.d_lr,
                                          betas=(opts.beta_1, 0.99))

        num_batches = len(dloader) 

        #self.load_weights()

        self.G.train()
        self.D.train()

        l1_weight = l1_init
        global_step = 1
        timings = []
        noisy_samples = None
        clean_samples = None
        z_sample = Variable(torch.randn(20, self.z_dim))
        if self.do_cuda:
            z_sample = z_sample.cuda()
        for epoch in range(1, opts.epoch + 1):
            beg_t = timeit.default_timer()
            for bidx, batch in enumerate(dloader, start=1):
                if epoch >= l1_dec_epoch:
                    if l1_weight > 0:
                        l1_weight -= l1_dec_step
                        # ensure it is 0 if it goes < 0
                        l1_weight = max(0, l1_weight)
                clean, noisy = batch
                clean = Variable(clean.unsqueeze(1))
                noisy = Variable(noisy.unsqueeze(1))
                lab = Variable(torch.ones(clean.size(0)))
                if self.do_cuda:
                    clean = clean.cuda()
                    noisy = noisy.cuda()
                    lab = lab.cuda()
                if noisy_samples is None:
                    noisy_samples = noisy[:20, :, :]
                    clean_samples = clean[:20, :, :]
                # (1) D real update
                Dopt.zero_grad()
                D_in = torch.cat((clean, noisy), dim=1)
                d_real = self.D(D_in)
                d_real_loss = criterion(d_real, lab)
                d_real_loss.backward()
                
                # (2) D fake update
                Genh = self.G(noisy)
                D_fake_in = torch.cat((Genh.detach(), noisy), dim=1)
                d_fake = self.D(D_fake_in)
                lab.data.fill_(0)
                d_fake_loss = criterion(d_fake, lab)
                d_fake_loss.backward()
                Dopt.step()
                d_loss = d_fake_loss  + d_real_loss

                # (3) G real update
                Gopt.zero_grad()
                lab.data.fill_(1)
                d_fake_ = self.D(torch.cat((Genh, noisy), dim=1))
                g_adv_loss = criterion(d_fake_, lab)
                g_l1_loss = l1_weight * F.l1_loss(Genh, clean)
                g_loss = g_adv_loss + g_l1_loss
                g_loss.backward()
                Gopt.step()

                if bidx % log_freq == 0 or bidx >= len(dloader):
                    d_real_loss_v = np.asscalar(d_real_loss.cpu().data.numpy())
                    d_fake_loss_v = np.asscalar(d_fake_loss.cpu().data.numpy())
                    g_adv_loss_v = np.asscalar(g_adv_loss.cpu().data.numpy())
                    g_l1_loss_v = np.asscalar(g_l1_loss.cpu().data.numpy())
                    end_t = timeit.default_timer()
                    timings.append(end_t - beg_t)
                    beg_t = timeit.default_timer()
                    print('(Iter {}) Batch {}/{} (Epoch {}) d_real:{:.4f}, '
                          'd_fake:{:.4f}, g_adv:{:.4f}, g_l1:{:.4f} '
                          'l1_w: {:.3f}, btime: {:.4f} s, mbtime: {:.4f} s'
                          ''.format(global_step, bidx, len(dloader),
                                    epoch, d_real_loss_v, 
                                    d_fake_loss_v, g_adv_loss_v,
                                    g_l1_loss_v, l1_weight, timings[-1],
                                    np.mean(timings)))
                    self.writer.add_scalar('D_real', d_real_loss_v,
                                           global_step)
                    self.writer.add_scalar('D_fake', d_fake_loss_v,
                                           global_step)
                    self.writer.add_scalar('G_adv', g_adv_loss_v,
                                           global_step)
                    self.writer.add_scalar('G_l1', g_l1_loss_v,
                                           global_step)
                    self.writer.add_histogram('Gz', Genh.cpu().data.numpy(),
                                              global_step, bins='sturges')
                    self.writer.add_histogram('clean', clean.cpu().data.numpy(),
                                              global_step, bins='sturges')
                    self.writer.add_histogram('noisy', noisy.cpu().data.numpy(),
                                              global_step, bins='sturges')
                    canvas_w = self.G(noisy_samples, z=z_sample)
                    sample_dif = noisy_samples - clean_samples
                    # sample wavs
                    for m in range(noisy_samples.size(0)):
                        m_canvas = de_emphasize(canvas_w[m,
                                                         0].cpu().data.numpy(),
                                                self.preemph)
                        print('w{} max: {} min: {}'.format(m,
                                                           m_canvas.max(),
                                                           m_canvas.min()))
                        wavfile.write(os.path.join(self.save_path,
                                                   'sample_{}-'
                                                   '{}.wav'.format(global_step,
                                                                   m)),
                                      int(16e3), m_canvas)
                        m_clean = de_emphasize(clean_samples[m,
                                                             0].cpu().data.numpy(),
                                               self.preemph)
                        m_noisy = de_emphasize(noisy_samples[m,
                                                             0].cpu().data.numpy(),
                                               self.preemph)
                        m_dif = de_emphasize(sample_dif[m,
                                                        0].cpu().data.numpy(),
                                             self.preemph)
                        m_gtruth_path = os.path.join(self.save_path,
                                                     'gtruth_{}.wav'.format(m))
                        if not os.path.exists(m_gtruth_path):
                            wavfile.write(os.path.join(self.save_path,
                                                       'gtruth_{}.wav'.format(m)),
                                          int(16e3), m_clean)
                            wavfile.write(os.path.join(self.save_path,
                                                       'noisy_{}.wav'.format(m)),
                                          int(16e3), m_noisy)
                            wavfile.write(os.path.join(self.save_path,
                                                       'dif_{}.wav'.format(m)),
                                          int(16e3), m_dif)
                    # save model
                    self.save(self.save_path, global_step)
                global_step += 1

            if va_dloader is not None:
                #pesqs, mpesq = self.evaluate(opts, va_dloader, log_freq)
                pesqs, npesqs, \
                mpesq, mnpesq, \
                ssnrs, nssnrs, \
                mssnr, mnssnr = self.evaluate(opts, va_dloader, 
                                                log_freq, do_noisy=True)
                print('mean noisyPESQ: ', mnpesq)
                print('mean GenhPESQ: ', mpesq)
                self.writer.add_scalar('noisyPESQ', mnpesq, epoch)
                self.writer.add_scalar('noisySSNR', mnssnr, epoch)
                self.writer.add_scalar('GenhPESQ', mpesq, epoch)
                self.writer.add_scalar('GenhSSNR', mssnr, epoch)
                #self.writer.add_histogram('noisyPESQ', npesqs,
                #                          epoch, bins='sturges')
                #self.writer.add_histogram('GenhPESQ', pesqs,
                #                          epoch, bins='sturges')


    def evaluate(self, opts, dloader, log_freq, do_noisy=False,
                 max_samples=100):
        """ Objective evaluation with PESQ and SSNR """
        self.G.eval()
        self.D.eval()
        beg_t = timeit.default_timer()
        pesqs = []
        ssnrs = []
        if do_noisy:
            npesqs = []
            nssnrs = []
        total_s = 0
        timings = []
        # going over dataset ONCE
        for bidx, batch in enumerate(dloader, start=1):
            clean, noisy = batch
            clean = Variable(clean, volatile=True)
            noisy = Variable(noisy.unsqueeze(1), volatile=True)
            if self.do_cuda:
                clean = clean.cuda()
                noisy = noisy.cuda()
            Genh = self.G(noisy).squeeze(1)
            clean_npy = clean.cpu().data.numpy()
            if do_noisy:
                noisy_npy = noisy.cpu().data.numpy()
            Genh_npy = Genh.cpu().data.numpy()
            for sidx in range(Genh.size(0)):
                clean_utt = denormalize_wave_minmax(clean_npy[sidx]).astype(np.int16)
                clean_utt = clean_utt.reshape(-1)
                Genh_utt = denormalize_wave_minmax(Genh_npy[sidx]).astype(np.int16)
                Genh_utt = Genh_utt.reshape(-1)
                # compute PESQ per file
                pesq = PESQ(clean_utt, Genh_utt)
                if 'error' in pesq:
                    print('Skipping error')
                    continue
                pesq = float(pesq)
                pesqs.append(pesq)
                snr_mean, segsnr_mean = SSNR(clean_utt, Genh_utt)
                segsnr_mean = float(segsnr_mean)
                ssnrs.append(segsnr_mean)
                print('Genh sample {} > PESQ: {:.3f}, SSNR: {:.3f} dB'
                      ''.format(total_s, pesq, segsnr_mean))
                if do_noisy:
                    # noisy PESQ too
                    noisy_utt = denormalize_wave_minmax(noisy_npy[sidx]).astype(np.int16)
                    noisy_utt = noisy_utt.reshape(-1)
                    npesq = PESQ(clean_utt, noisy_utt)
                    npesq = float(npesq)
                    npesqs.append(npesq)
                    nsnr_mean, nsegsnr_mean = SSNR(clean_utt, noisy_utt)
                    nsegsnr_mean = float(nsegsnr_mean)
                    nssnrs.append(nsegsnr_mean)
                    print('Noisy sample {} > PESQ: {:.3f}, SSNR: {:.3f} dB'
                          ''.format(total_s, npesq, nsegsnr_mean))
                # Segmental SNR
                total_s += 1
                end_t = timeit.default_timer()
                timings.append(end_t - beg_t)
                print('Mean pesq computation time: {}'
                      's'.format(np.mean(timings)))
                beg_t = timeit.default_timer()
                #print('{} PESQ: {}'.format(sidx, pesq))
                #wavfile.write('{}_clean_test.wav'.format(sidx), 16000,
                #              clean_utt)
                #wavfile.write('{}_enh_test.wav'.format(sidx), 16000,
                #              Genh_utt)
            #if bidx % log_freq == 0 or bidx >= len(dloader):
            #    print('EVAL Batch {}/{} mPESQ: {:.4f}'
            #          ''.format(bidx,
            #                    len(dloader),
            #                    np.mean(pesqs)))
            if total_s >= max_samples:
                break
        return np.array(pesqs), np.array(npesqs), np.mean(pesqs), \
               np.mean(npesqs), np.array(ssnrs), \
               np.array(nssnrs), np.mean(ssnrs), np.mean(nssnrs)

