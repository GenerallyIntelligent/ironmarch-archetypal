from ray.tune import schedulers
import torch
from torch.utils.data import DataLoader

from datasets import IronMarch, BertPreprocess
from models import VAE
from side_information import SideLoader

from torch.optim import Adam

import os

import geomloss

from ray import tune
from ray.tune.suggest import ConcurrencyLimiter
from ray.tune.schedulers import AsyncHyperBandScheduler
from ray.tune.suggest.optuna import OptunaSearch

from training.search_space import config

from filelock import FileLock

bert_embedding_size = 768

def train_sinkhorn_vae(config, checkpoint_dir = None):
    device = config['device']
    if device == 'cuda:0':
        if not torch.cuda.is_available():
            print('Could not find GPU, reverting to CPU training!')
            device = 'cpu'
    
    train_loader, val_loader, class_proportions = get_datasets(config)

    if config['dataset']['context']:
        input_dim = bert_embedding_size * 2
    else:
        input_dim = bert_embedding_size
    model = VAE(
        latent_dim = config['model']['latent_dims'],
        input_dim = input_dim,
        feature_dim = 7,
        use_softmax = config['model']['softmax']
    )
    model.to(device)

    optimizer = Adam(
        parameters = model.parameters(), 
        lr = config['adam']['learning_rate'], 
        betas = (config['adam']['betas'][0], config['adam']['betas'][1])
    )

    if checkpoint_dir:
        checkpoint = os.path.join(checkpoint_dir, "checkpoint")
        model_state, optimizer_state = torch.load(checkpoint)
        model.load_state_dict(model_state)
        optimizer.load_state_dict(optimizer_state)

    sinkhorn_loss_fn = geomloss.SamplesLoss(
        loss = config['losses']['distribution']['type'], 
        p = config['losses']['distribution']['p'], 
        blur = config['losses']['distribution']['blur'])
    reconstruction_loss_fn = torch.nn.MSELoss()
    class_loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight = (1 / class_proportions) * config['losses']['class']['bias'])

    dirichlet_distribution = torch.distributions.dirichlet.Dirichlet(torch.tensor([config['losses']['distribution']['alpha'] for i in range(config['model']['latent_dims'])]))

    for epoch_num in range(config['training']['max_epochs']):
        for batch_num, batch in enumerate(train_loader):
            features = batch['features'].to(device).float()
            posts = batch['posts'].to(device).float()

            augmented_posts = posts + torch.normal(mean = 0.0, std = config['latent_space']['noise']['std'], size = posts.shape).to(device)

            optimizer.zero_grad()

            mean, logvar = model.encode(augmented_posts)
            eps = model.reparameterize(mean, logvar)
            logits = model.decoder(eps)
            feature_predictions = model.feature_head(eps)

            sampled_dirichlet = dirichlet_distribution.sample([config['training']['batch_size']]).to(device)
            distribution_loss = sinkhorn_loss_fn(eps, sampled_dirichlet) 

            reconstruction_loss = reconstruction_loss_fn(posts, logits)

            class_loss = class_loss_fn(feature_predictions, features)

            loss = class_loss * config['losses']['class']['weight'] + distribution_loss * config['losses']['distribution']['weight'] + reconstruction_loss * config['losses']['reconstruction']['weight']

            loss.backward()
            optimizer.step()

        loss_accum = 0
        class_accum = 0
        dist_accum = 0
        recon_accum = 0
        
        tp_accum = 0
        fp_accum = 0
        tn_accum = 0
        fn_accum = 0

        with torch.no_grad():
            for batch_num, batch in enumerate(val_loader):
                features = batch['features'].to(device).float()
                posts = batch['posts'].to(device).float()

                mean, logvar = model.encode(posts)
                eps = model.reparameterize(mean, logvar)
                logits = model.decoder(eps)
                feature_predictions = model.feature_head(eps)

                sampled_dirichlet = dirichlet_distribution.sample([config['training']['batch_size']]).to(device)
                distribution_loss = sinkhorn_loss_fn(eps, sampled_dirichlet) 

                reconstruction_loss = reconstruction_loss_fn(posts, logits)

                class_loss = class_loss_fn(feature_predictions, features)

                loss = class_loss * config['losses']['class']['weight'] + distribution_loss * config['losses']['distribution']['weight'] + reconstruction_loss * config['losses']['reconstruction']['weight']

                loss_accum += loss.detach().item()
                class_accum += class_loss.detach().item()
                dist_accum += distribution_loss.detach().item()
                recon_accum += reconstruction_loss.detach().item()

                binary_predictions = torch.ge(feature_predictions, config['losses']['class']['threshold'])
                tp_accum += ((binary_predictions == 1.0) & (features == 1.0)).detach().sum().item()
                fp_accum += ((binary_predictions == 1.0) & (features == 0.0)).detach().sum().item()
                tn_accum += ((binary_predictions == 0.0) & (features == 0.0)).detach().sum().item()
                fn_accum += ((binary_predictions == 0.0) & (features == 1.0)).detach().sum().item()

        yield {
            'total_loss' : loss_accum / (batch_num + 1),
            'class_loss' : class_accum / (batch_num + 1),
            'dist_loss' : dist_accum / (batch_num + 1),
            'recon_loss' : recon_accum / (batch_num + 1),
            'precision' : tp_accum / (tp_accum + fp_accum),
            'recall' : tp_accum / (tp_accum + fn_accum),
            'accuracy' : (tp_accum + tn_accum) / (tp_accum + fn_accum + tn_accum + fn_accum),
            'specificity' : tn_accum / (tn_accum + fp_accum),
            'f1_score' : tp_accum / (tp_accum + (0.5))
        }

def get_datasets(config):
    with FileLock(config['dataset']['directory'] + '.lock'):
        side_information_file_paths = config['dataset']['side_information']['file_paths']
        side_information_loader = SideLoader(side_information_file_paths)

        bert_tokenizer = torch.hub.load('huggingface/pytorch-transformers', 'tokenizer', 'roberta-base')
        bert = torch.hub.load('huggingface/pytorch-transformers', 'model', 'roberta-base')
        preprocessing_fn = BertPreprocess(bert = bert, tokenizer = bert_tokenizer, device = config['device'])
        dataset = IronMarch(
            dataroot = config['dataset']['directory'],
            preprocessing_function = preprocessing_fn,
            side_information = side_information_loader,
            use_context = config['dataset']['context'],
            cache = True
        )
        del bert, bert_tokenizer

        train_sampler, val_sampler = dataset.split_validation(validation_split = 0.1)
        train_loader = DataLoader(dataset, batch_size = config['dataset']['batch_size'], sampler = train_sampler)
        val_loader = DataLoader(dataset, batch_size = config['dataset']['batch_size'], sampler = val_sampler)

        class_proportions = dataset.get_class_proportions()

    return train_loader, val_loader, class_proportions
    
def run_optuna_tune(smoke_test = True):
    algorithm = OptunaSearch()
    algorithm = ConcurrencyLimiter(algorithm, max_concurrent = 6)
    scheduler = AsyncHyperBandScheduler()
    analysis = tune.run(
        train_sinkhorn_vae,
        metric = 'total_loss',
        mode = 'min',
        search_alg = algorithm,
        scheduler = scheduler,
        num_samples = 2 if smoke_test else 20,
        config = config,
        local_dir = 'results',
        fail_fast = True
    )

if __name__ == '__main__':
    run_optuna_tune(smoke_test = True)
