/**
 * API 聚合出口：按域一文件（auth/accounts/keys/...），本文件只做显式 re-export。
 * 各页面片在自己的 `lib/api/<domain>.ts` 里实现后，来这里加一行 re-export。
 */
export {http, errText, get, post, patch, put, del} from './http';
export {authApi} from './auth';
