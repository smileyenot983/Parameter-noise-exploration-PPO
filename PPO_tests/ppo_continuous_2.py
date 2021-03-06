# -*- coding: utf-8 -*-
"""PPO_continuous.ipynb

Automatically generated by Colaboratory.
#
Original file is located at
    https://colab.research.google.com/drive/1IHLuCX5n4nmBU79IhA2tM4HRsjHnmXEN
"""

import torch
import torch.nn as nn
from torch.distributions import MultivariateNormal
from torch.distributions.normal import Normal

import gym
import numpy as np
import matplotlib.pyplot as plt
from torch.distributions.kl import kl_divergence
import torch.nn.functional as F

# custom environment for gym
import math

import cartpole_swingup
import pybulletgym

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class Memory:
    def __init__(self):
        self.actions = []
        self.states = []
        self.logprobs = []
        self.rewards = []
        self.is_terminals = []

    def clear_memory(self):
        self.actions = []
        self.states = []
        self.logprobs = []
        self.rewards = []
        self.is_terminals = []


class ActorCriticLayerNorm(nn.Module):
    def __init__(self, state_dim, action_dim, n_latent_var, action_std):
        super(ActorCriticLayerNorm, self).__init__()

        # actor
        self.action_layer = nn.Sequential(
            nn.Linear(state_dim, n_latent_var),

            # layer normalization => (feature - mean(features))/std(features)
            # in contrast to batch norm, layer computes mean and average across features of one sample
            nn.LayerNorm(n_latent_var),
            nn.Tanh(),

            nn.Linear(n_latent_var, n_latent_var),
            nn.LayerNorm(n_latent_var),
            nn.Tanh(),

            nn.Linear(n_latent_var, action_dim),
            # nn.Softmax()

            nn.LayerNorm(action_dim),
            nn.Tanh(),

        )

        # critic
        self.value_layer = nn.Sequential(
            nn.Linear(state_dim, n_latent_var),
            nn.Tanh(),
            nn.Linear(n_latent_var, n_latent_var),
            nn.Tanh(),
            nn.Linear(n_latent_var, 1)
        )

        self.action_var = torch.full((action_dim,), action_std * action_std).to(device)

    def act(self, state, memory):
        state = torch.from_numpy(state).float().to(device)

        # get mean of action
        action_mean = self.action_layer(state)

        cov_mat = torch.diag(self.action_var, ).to(device)

        # multidimensional normal distribution with mean=action_mean, variance=cov_mat
        action_distribution = MultivariateNormal(action_mean, cov_mat)

        action = action_distribution.sample()

        # logarithm of probability of chosen action
        log_prob = action_distribution.log_prob(action)

        # saving data for monte carlo update
        memory.states.append(state)
        memory.actions.append(action)
        memory.logprobs.append(log_prob)

        return action.cpu().data.numpy()

    def evaluate(self, state, action):
        action_mean = self.action_layer(state)

        # variance of each action
        action_var = self.action_var.expand_as(action_mean)

        # covariance matrix with action's variances on main diagonal
        cov_mat = torch.diag_embed(action_var).to(device)

        # creating distribution to calculate entropy and logprobs
        action_distribution = MultivariateNormal(action_mean, cov_mat)

        action_logprobs = action_distribution.log_prob(action)
        distribution_entropy = action_distribution.entropy()
        state_value = self.value_layer(state)

        return action_logprobs, torch.squeeze(state_value), distribution_entropy

class ActorCritic(nn.Module):
    def __init__(self, state_dim, action_dim, n_latent_var, action_std):
        super(ActorCritic, self).__init__()

        # actor
        self.action_layer = nn.Sequential(
            nn.Linear(state_dim, n_latent_var),

            # layer normalization => (feature - mean(features))/std(features)
            # in contrast to batch norm, layer computes mean and average across features of one sample
            # nn.LayerNorm(n_latent_var),
            nn.Tanh(),

            nn.Linear(n_latent_var, n_latent_var),
            # nn.LayerNorm(n_latent_var),
            nn.Tanh(),

            nn.Linear(n_latent_var, action_dim),
            # nn.Softmax()

            # nn.LayerNorm(action_dim),
            nn.Tanh(),

        )

        # critic
        self.value_layer = nn.Sequential(
            nn.Linear(state_dim, n_latent_var),
            nn.Tanh(),
            nn.Linear(n_latent_var, n_latent_var),
            nn.Tanh(),
            nn.Linear(n_latent_var, 1)
        )

        self.action_var = torch.full((action_dim,), action_std * action_std).to(device)

    def act(self, state, memory):
        state = torch.from_numpy(state).float().to(device)

        # get mean of action
        action_mean = self.action_layer(state)

        cov_mat = torch.diag(self.action_var, ).to(device)

        # multidimensional normal distribution with mean=action_mean, variance=cov_mat
        action_distribution = MultivariateNormal(action_mean, cov_mat)

        action = action_distribution.sample()

        # logarithm of probability of chosen action
        log_prob = action_distribution.log_prob(action)

        # saving data for monte carlo update
        memory.states.append(state)
        memory.actions.append(action)
        memory.logprobs.append(log_prob)

        return action.cpu().data.numpy()

    def evaluate(self, state, action):
        action_mean = self.action_layer(state)

        # variance of each action
        action_var = self.action_var.expand_as(action_mean)

        # covariance matrix with action's variances on main diagonal
        cov_mat = torch.diag_embed(action_var).to(device)

        # creating distribution to calculate entropy and logprobs
        action_distribution = MultivariateNormal(action_mean, cov_mat)

        action_logprobs = action_distribution.log_prob(action)
        distribution_entropy = action_distribution.entropy()
        state_value = self.value_layer(state)

        return action_logprobs, torch.squeeze(state_value), distribution_entropy

class PPO:
    def __init__(self, state_dim, action_dim, n_latent_var, K_epochs,layer_norm):
        self.lr = 0.002
        self.betas = (0.9, 0.999)
        self.gamma = 0.99
        self.eps_clip = 0.2

        self.K_epochs = K_epochs

        self.mse_loss = nn.MSELoss()

        self.action_std = 0.5

        if layer_norm:
        # we have 2 policies old and new
            self.policy_new = ActorCriticLayerNorm(state_dim, action_dim, n_latent_var, self.action_std).to(device)
            self.policy_old = ActorCriticLayerNorm(state_dim, action_dim, n_latent_var, self.action_std).to(device)
        else:
            self.policy_new = ActorCritic(state_dim, action_dim, n_latent_var, self.action_std).to(device)
            self.policy_old = ActorCritic(state_dim, action_dim, n_latent_var, self.action_std).to(device)

        self.optimizer = torch.optim.Adam(self.policy_new.parameters(), lr=self.lr, betas=self.betas)

        # synchronizing 2 neural networks in the beginning
        self.policy_old.load_state_dict(self.policy_new.state_dict())


    def update(self, memory):

        gamma = self.gamma
        # creating list of discounted rewards
        discounted_rewards = []
        discounted_reward = 0

        for reward, is_terminal in zip(reversed(memory.rewards), reversed(memory.is_terminals)):
            if is_terminal:
                discounted_reward = 0
            discounted_reward = reward + gamma * discounted_reward
            discounted_rewards.append(discounted_reward)

        discounted_rewards = discounted_rewards[::-1]

        discounted_rewards = torch.tensor(discounted_rewards).to(device)
        # normalizing: mean = 0, std = 1
        discounted_rewards = (discounted_rewards - torch.mean(discounted_rewards)) / (
                torch.std(discounted_rewards) + 1e-5)

        # creating tensor from list
        old_states = torch.stack(memory.states).to(device).detach()
        old_actions = torch.stack(memory.actions).to(device).detach()
        old_logprobs = torch.stack(memory.logprobs).to(device).detach()

        # gradient ascent for K epochs
        for i in range(self.K_epochs):
            logprobs, state_values, dist_entropy = self.policy_new.evaluate(old_states, old_actions)

            # calculating policy_new/policy_old ratio
            ratios = torch.exp(logprobs - old_logprobs.detach())

            # surrogate loss
            advantages = discounted_rewards - state_values

            #
            surr1 = advantages * ratios
            surr2 = torch.clamp(ratios, min=1 - self.eps_clip, max=1 + self.eps_clip) * advantages

            loss = -torch.min(surr1, surr2) + 0.5 * self.mse_loss(discounted_rewards,
                                                                  state_values) - 0.01 * dist_entropy

            # backprop
            self.optimizer.zero_grad()
            loss.mean().backward()
            self.optimizer.step()

        # new policy becomes old policy
        self.policy_old.load_state_dict(self.policy_new.state_dict())



# adding noise to perturbate policy parameters
def add_noise(policy, std):
    n_normalized_layers = 2
    noisy_layers=0
    for layer in policy.action_layer:
        if noisy_layers == n_normalized_layers:
            break
        if isinstance(layer,nn.Linear):
            #generating noise with given std
            noise = torch.normal(mean=0,std=std,size=(layer.weight.shape))

            with torch.no_grad():
                layer.weight += noise
            noisy_layers += 1
    # we don't have to return anything because function modifies parameters in place


def train(test_env = 'InvertedDoublePendulum-v2',max_episodes=5000, add_parameter_noise=False,pretrained=False,layer_norm=False):

    env_name = test_env
    # creating environment
    env = gym.make(env_name)
    state_dim = env.observation_space.shape[0]
    action_dim = env.action_space.shape[0]
    print("action_dim: " + str(action_dim))
    render = False

    log_interval = 50  # print avg reward in the interval
    max_episodes = max_episodes  # max training episodes
    max_timesteps = 300  # max timesteps in one episode
    n_latent_var = 64  # number of variables in hidden layer
    update_timestep = 500  # update policy every n timesteps
    lr = 1e-3
    betas = (0.9, 0.999)
    gamma = 0.99  # discount factor
    K_epochs = 1  # update policy for K epochs
    eps_clip = 0.2  # clip parameter for PPO
    random_seed = None
    action_std = 0.5

    # parameter noise hyperparameters
    noise_scalefactor = 1.01  # scaling factor for noise scaling
    distance_threshold = 1.0
    perturbation_timestep = 250
    sigma = 0.1

    if random_seed:
        torch.manual_seed(random_seed)
        env.seed(random_seed)

    memory = Memory()
    ppo = PPO(state_dim, action_dim, n_latent_var, K_epochs,layer_norm)
    # ppo = PPO(state_dim, action_dim,n_latent_var,lr,betas,gamma,K_epochs,eps_clip)
    print(lr, betas)

    weights_path = 'actor_critic_weights.h5'

    if pretrained:
        ppo.policy_old.load_state_dict(weights_path)
        ppo.policy_new.load_state_dict(weights_path)

    # logging variables
    running_reward = 0
    avg_length = 0
    timestep = 0


    if layer_norm:
        policy_perturbed = ActorCriticLayerNorm(state_dim, action_dim, n_latent_var, action_std).to(device)
    else:
        policy_perturbed = ActorCritic(state_dim, action_dim, n_latent_var, action_std).to(device)

    #list for episodic rewards
    rewards = []
    #list with means of several episodes(n_episodes = log_interval)
    plotted_rewards = []
    sigmas = []
    distances = []

    # training loop
    for i_episode in range(1, max_episodes + 1):
        if render:
            env.render()
        state = env.reset()
        episode_reward = 0
        done = False
        # while not done:
        for t in range(max_timesteps):
            timestep += 1

            # Running policy_old:
            action = ppo.policy_old.act(state, memory)
            state, reward, done, _ = env.step(action)
            if math.isnan(reward):
                print(action)
            # Saving reward and is_terminal:
            memory.rewards.append(reward)
            memory.is_terminals.append(done)

            # Saving rewards for plotting:
            episode_reward += reward


            # _________HERE IS PARAMETER NOISE PART______

            # adaptive noise scaling
            if add_parameter_noise and timestep % perturbation_timestep == 0:

                with torch.no_grad():
                    # copying current parameters of policy
                    policy_perturbed.load_state_dict(ppo.policy_old.state_dict())

                    # perturbating network by adding noise to paramerers

                    add_noise(policy_perturbed, sigma)

                    #defining another distance metric
                    states = torch.stack(memory.states).to(device).detach()

                    #action means
                    out_original = ppo.policy_old.action_layer(states)
                    out_perturbed = policy_perturbed.action_layer(states)
                    #action stds
                    # std_original, mean_original = torch.std_mean(out_original)
                    # std_perturbed, mean_perturbed = torch.std_mean(out_perturbed)
                    #
                    # distribution_original = torch.distributions.normal.Normal(loc = mean_original, scale = std_original)
                    # distribution_perturbed = torch.distributions.normal.Normal(loc=mean_perturbed, scale=std_perturbed)

                    distance = torch.mean(torch.sqrt((out_original - out_perturbed)**2))


                    # distance = torch.distributions.kl.kl_divergence(distribution_original,distribution_perturbed)

                    distances.append(distance.data.numpy())
                    # print("distance: " + str(distance))

                    if distance < distance_threshold:
                        # parameter_noise = [layer_noise * noise_scalefactor for layer_noise in parameter_noise]
                        sigma *= noise_scalefactor

                    elif distance > distance_threshold:
                        # parameter_noise = [layer_noise / noise_scalefactor for layer_noise in parameter_noise]
                        sigma /= noise_scalefactor

                    sigmas.append(sigma)

                    ppo.policy_old.load_state_dict(policy_perturbed.state_dict())
                    # print("sigma: " + str(sigma))
                    # update if its time

            if timestep % update_timestep == 0:
                ppo.update(memory)
                memory.clear_memory()
                timestep = 0

            running_reward += reward
            if render:
                env.render()
            if done:
                break

        #saving rewards and sigmas
        rewards.append(episode_reward)

        # logging
        if i_episode % log_interval == 0:
            # running_reward = np.mean(rewards[i_episode-log_interval:i_episode])
            # if math.isnan(running_reward):
            #     print(rewards)

            # rewards = []
            # plotted_rewards.append(running_reward)
            print('Episode {} \t  reward: {} \t'.format(i_episode, episode_reward ))


        weights_path = 'actor_critic_weights.h5'
        torch.save(ppo.policy_new.state_dict(),weights_path)

    if add_parameter_noise:
        return rewards,sigmas,distances
    else:
        return rewards


# env_name = "CartPoleSwingUp-v2"
# env_name = "InvertedDoublePendulumSparsePyBulletEnv-v0"
# env_name = "InvertedDoublePendulumPyBulletEnv-v0"
# env_name = 'AntPyBulletEnv-v0'

rewards = train(test_env= 'Pendulum-v0',add_parameter_noise=False, max_episodes=10000,pretrained=False,layer_norm=False)
# rewards_noise,sigmas,distances = train(test_env= 'Pendulum-v0',add_parameter_noise=True, max_episodes=10000,pretrained=False,layer_norm=True)

plot_step=100
rewards_noise_plot = []
for i in range(len(rewards_noise)/plot_step-1):
    rewards_noise_plot.append(np.mean(rewards_noise[i*plot_step:(i+1)*plot_step]))


t = np.arange(len(rewards_noise))
plt.plot(t, rewards_noise_plot,label='without noise')

# plt.legend()
plt.ylabel('Reward')
# plt.savefig('rewards_original.png')
# plt.show()

# plt.plot(t, rewards_noise,label='with noise')
# plt.ylabel('Reward')
plt.savefig('rewards_noisy.png')
plt.legend()
plt.show()


# t = np.arange(len(sigmas))
# plt.plot(t, sigmas,)
# # plt.scatter(t, sigmas_noise, label = 'with noise')
# plt.ylabel('Sigma')
# plt.savefig('sigmas.png')
# plt.show()
#
# t = np.arange(len(distances))
# plt.plot(t, distances)
# # plt.scatter(t, distances_noise)
# plt.ylabel('Distance')
# plt.savefig('distances.png')
# plt.show()