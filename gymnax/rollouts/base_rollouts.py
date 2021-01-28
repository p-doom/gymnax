import jax
import jax.numpy as jnp
from jax import lax, jit, vmap
from functools import partial


class BaseRollouts(object):
    """ Base wrapper for episode rollouts. """
    def __init__(self, step, reset, env_params):
        self.step = step
        self.reset = reset
        self.env_params = env_params
        self.max_steps_in_episode = env_params["max_steps_in_episode"]

    def action_selection(self, key, obs, agent_params, actor_state):
        """ Compute action to be executed in environment. """
        raise NotImplementedError

    def prepare_experience(self, env_output, actor_state):
        """ Prepare the generated data (net/env) to be stored in a buffer. """
        raise NotImplementedError

    def store_experience(self, step_experience):
        """ Store the transition data (net + env) in a buffer. """
        raise NotImplementedError

    def update_learner(self, agent_params, learner_state):
        """ Perform an update to the parameters of the learner. """
        raise NotImplementedError

    def init_learner_state(self, agent_params):
        """ Initialize the state of the learner (e.g. optimizer). """
        raise NotImplementedError

    def init_actor_state(self):
        """ Initialize the state of the actor (e.g. for exploration). """
        raise NotImplementedError

    def init_collector(self, agent_params):
        """ Initialize the rollout collector/learning dojo. """
        self.agent_params = agent_params
        self.learner_state = self.init_learner_state(agent_params)
        self.actor_state = self.init_actor_state()

    def perform_transition(self, key, env_params, state, action):
        """ Perform the step transition in the environment. """
        next_obs, next_state, reward, done, _ = self.step(key, env_params,
                                                          state, action)
        return next_obs, next_state, reward, done, _

    def actor_learner_step(self, carry_input, tmp):
        """ lax.scan compatible step transition in JAX env.
            This implements an alternating actor-learner paradigm for
            each step transition in the environment. Rewrite for case of
            On-Policy methods and update at end of episode.
        """
        # 0. Unpack carry, split rng key for action selection + transition
        rng, obs, state, env_params = carry_input[0:4]
        agent_params, actor_state, learner_state = carry_input[4:7]
        rng, key_act, key_step = jax.random.split(rng, 3)

        # 1. Perform action selection using actor NN
        action, actor_state = self.action_selection(key_act, obs,
                                                    agent_params,
                                                    actor_state)

        # 2. Perform the step transition in the environment & format env output
        next_obs, next_state, reward, done, _ = self.perform_transition(
                                    key_step, env_params, state, action)
        env_output = (state, next_state, obs, next_obs, action, reward, done)

        # 3. Prepare gathered info from transition (env + net) [keep state info]
        step_experience = self.prepare_experience(env_output, actor_state)

        # 4. Store the transition in a transition buffer
        self.store_experience(step_experience)

        # 5. Update the learner by e.g. performing some SGD update
        agent_params, learner_state = self.update_learner(agent_params,
                                                          learner_state)

        # 6. Collect all relevant data for next actor-learner-step
        carry, y = ([rng, next_obs.squeeze(), next_state.squeeze(),
                     env_params, agent_params, actor_state, learner_state],
                    [reward])
        return carry, y

    @partial(jit, static_argnums=(0, 3))
    def lax_rollout(self, key_input, env_params, max_steps_in_episode,
                    agent_params, actor_state, learner_state):
        """ Rollout a gymnax episode with lax.scan. """
        obs, state = self.reset(key_input, env_params)
        scan_out1, scan_out2 = lax.scan(
                            self.actor_learner_step,
                            [key_input, obs, state, env_params,
                             agent_params, actor_state, learner_state],
                            [jnp.zeros(max_steps_in_episode)])
        return scan_out1, jnp.array(scan_out2).squeeze()

    @partial(jit, static_argnums=(0, 3))
    def vmap_rollout(self, key_input, env_params, max_steps_in_episode,
                     agent_params, actor_state, learner_state):
        """ Jit + vmap wrapper around scanned episode rollout. """
        rollout_map = vmap(self.lax_rollout,
                           in_axes=(0, None, None, None, None, None),
                           out_axes=0)
        traces, rewards = rollout_map(key_input, self.env_params,
                                      self.max_steps_in_episode,
                                      self.agent_params,
                                      self.actor_state, self.learner_state)
        return traces, rewards

    def episode_rollout(self, key_rollout):
        """ Jitted episode rollout for single episode. """
        try:
            trace, reward = self.lax_rollout(key_rollout,
                                             self.env_params,
                                             self.max_steps_in_episode,
                                             self.agent_params,
                                             self.actor_state,
                                             self.learner_state)
        except AttributeError as err:
            raise AttributeError(f"{err}. You need to initialize the "
                                  "agent's parameters and the states "
                                  "of the actor and learner.")
        return trace, reward

    def batch_rollout(self, key_rollout):
        """ Vmapped episode rollout for set of episodes. """
        try:
            traces, rewards = self.vmap_rollout(key_rollout,
                                                self.env_params,
                                                self.max_steps_in_episode,
                                                self.agent_params,
                                                self.actor_state,
                                                self.learner_state)
        except AttributeError as err:
            raise AttributeError(f"{err}. You need to initialize the "
                                  "agent params and actor/learner states.")
        return traces, rewards
