import { createRouter, createWebHistory } from 'vue-router'
import ChatView from '../views/ChatView.vue'
import KnowledgeView from '../views/KnowledgeView.vue'
import EvalView from '../views/EvalView.vue'

const routes = [
  { path: '/', redirect: '/chat' },
  { path: '/chat', name: 'chat', component: ChatView },
  { path: '/knowledge', name: 'knowledge', component: KnowledgeView },
  { path: '/eval', name: 'eval', component: EvalView },
  { path: '/:pathMatch(.*)*', redirect: '/chat' }
]

export default createRouter({
  history: createWebHistory(),
  routes
})
