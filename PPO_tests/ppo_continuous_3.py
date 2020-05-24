import torch
import torch.nn as nn
from torch.distributions import MultivariateNormal
import gym
import numpy as np

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class Memory:
    def __init__(self):
        self.actions = []
        self.states = []
        self.logprobs = []
        self.rewards = []
        self.is_terminals = []

    def clear_memory(self):
        del self.actions[:]
        del self.states[:]
        del self.logprobs[:]
        del self.rewards[:]
        del self.is_terminals[:]


class ActorCritic(nn.Module):
    def __init__(self, state_dim, action_dim, action_std):
        super(ActorCritic, self).__init__()
        # action mean range -1 to 1
        self.actor = nn.Sequential(
            nn.Linear(state_dim, 64),
            nn.Tanh(),
            nn.Linear(64, 32),
            nn.Tanh(),
            nn.Linear(32, action_dim),
            nn.Tanh()
        )
        # critic
        self.critic = nn.Sequential(
            nn.Linear(state_dim, 64),
            nn.Tanh(),
            nn.Linear(64, 32),
            nn.Tanh(),
            nn.Linear(32, 1)
        )
        self.action_var = torch.full((action_dim,), action_std * action_std).to(device)

    def forward(self):
        raise NotImplementedError

    def act(self, state, memory):
        action_mean = self.actor(state)
        cov_mat = torch.diag(self.action_var).to(device)

        dist = MultivariateNormal(action_mean, cov_mat)
        action = dist.sample()
        action_logprob = dist.log_prob(action)

        memory.states.append(state)
        memory.actions.append(action)
        memory.logprobs.append(action_logprob)

        return action.detach()

    def evaluate(self, state, action):
        action_mean = self.actor(state)

        action_var = self.action_var.expand_as(action_mean)
        cov_mat = torch.diag_embed(action_var).to(device)

        dist = MultivariateNormal(action_mean, cov_mat)

        action_logprobs = dist.log_prob(action)
        dist_entropy = dist.entropy()
        state_value = self.critic(state)

        return action_logprobs, torch.squeeze(state_value), dist_entropy


class PPO:
    def __init__(self, state_dim, action_dim, n_latent_var, K_epochs):
        self.lr = 0.002
        self.betas = (0.9, 0.999)
        self.gamma = 0.99
        self.eps_clip = 0.2

        self.K_epochs = K_epochs

        self.mse_loss = nn.MSELoss()

        self.action_std = 0.5

        # we have 2 policies old and new
        self.policy_new = ActorCritic(state_dim, action_dim, self.action_std).to(device)
        self.policy_old = ActorCritic(state_dim, action_dim, self.action_std).to(device)

        self.optimizer = torch.optim.Adam(self.policy_new.parameters(), lr=self.lr, betas=self.betas)

        # synchronizing 2 neural networks in the beginning
        self.policy_old.load_state_dict(self.policy_new.state_dict())

    def select_action(self, state, memory):
        state = torch.FloatTensor(state.reshape(1, -1)).to(device)
        return self.policy_old.act(state, memory).cpu().data.numpy().flatten()

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


def main():
    ############## Hyperparameters ##############
    env_name = "Pendulum-v0"
    render = False
    solved_reward = 300  # stop training if avg_reward > solved_reward
    log_interval = 20  # print avg reward in the interval
    max_episodes = 10000  # max training episodes
    max_timesteps = 200  # max timesteps in one episode

    update_timestep = 4000  # update policy every n timesteps
    action_std = 0.5  # constant std for action distribution (Multivariate Normal)
    K_epochs = 80  # update policy for K epochs
    eps_clip = 0.2  # clip parameter for PPO
    gamma = 0.99  # discount factor
    n_latent_var = 64
    lr = 0.0003  # parameters for Adam optimizer
    betas = (0.9, 0.999)

    random_seed = None
    #############################################

    # creating environment
    env = gym.make(env_name)
    state_dim = env.observation_space.shape[0]
    action_dim = env.action_space.shape[0]

    if random_seed:
        print("Random Seed: {}".format(random_seed))
        torch.manual_seed(random_seed)
        env.seed(random_seed)
        np.random.seed(random_seed)

    memory = Memory()
    ppo = PPO(state_dim, action_dim, n_latent_var, K_epochs)
    print(lr, betas)

    # logging variables
    running_reward = 0
    avg_length = 0
    time_step = 0

    # training loop
    for i_episode in range(1, max_episodes + 1):
        state = env.reset()
        for t in range(max_timesteps):
            time_step += 1
            # Running policy_old:
            action = ppo.select_action(state, memory)
            state, reward, done, _ = env.step(action)

            # Saving reward and is_terminals:
            memory.rewards.append(reward)
            memory.is_terminals.append(done)

            # update if its time
            if time_step % update_timestep == 0:
                ppo.update(memory)
                memory.clear_memory()
                time_step = 0
            running_reward += reward
            if render:
                env.render()
            if done:
                break

        avg_length += t

        # stop training if avg_reward > solved_reward
        # if running_reward > (log_interval * solved_reward):
        #     print("########## Solved! ##########")
        #     torch.save(ppo.policy.state_dict(), './PPO_continuous_solved_{}.pth'.format(env_name))
        #     break

        # save every 500 episodes
        if i_episode % 500 == 0:
            torch.save(ppo.policy_old.state_dict(), './PPO_continuous_{}.pth'.format(env_name))

        # logging
        if i_episode % log_interval == 0:
            avg_length = int(avg_length / log_interval)
            running_reward = int((running_reward / log_interval))

            print('Episode {} \t Avg length: {} \t Avg reward: {}'.format(i_episode, avg_length, running_reward))
            running_reward = 0
            avg_length = 0


if __name__ == '__main__':
    main()