from datetime import datetime
from glob import glob
from multiprocessing import Pool
from os.path import join
from re import findall, search
from statistics import mean
import matplotlib.pyplot as plt

from benchmark.utils import Print


class ParseError(Exception):
    pass


class LogParser:
    def __init__(self,nodes, faults, protocol, ddos):

        assert all(isinstance(x, str) for x in nodes)

        self.protocol = protocol
        self.ddos = ddos
        self.faults = faults
        self.committee_size = len(nodes)

        # Parse the nodes logs.
        try:
            with Pool() as p:
                results = p.map(self._parse_nodes, nodes)
        except (ValueError, IndexError) as e:
            raise ParseError(f'Failed to parse node logs: {e}')
        commit_epoch_batchid,nocounts,epochcounts,batchs,proposals, commits,configs = zip(*results)
        self.commit_epoch_batchid=self._merge_results([x.items() for x in commit_epoch_batchid])
        self.nocounts=self._merge_results([x.items() for x in nocounts])
        self.epochcounts=self._merge_results([x.items() for x in epochcounts])
        self.proposals = self._merge_results([x.items() for x in proposals])
        self.commits = self._merge_results([x.items() for x in commits])
        self.batchs = self._merge_results([x.items() for x in batchs])
        self.configs = configs[0]

    def _merge_results(self, input):
        # Keep the earliest timestamp.
        merged = {}
        for x in input:
            for k, v in x:
                if not k in merged or merged[k] < v:
                    merged[k] = v
        return merged

    def _parse_nodes(self, log):
        if search(r'panic', log) is not None:
            raise ParseError('Client(s) panicked')

        tmp = findall(r'\[INFO] (.*) core.* can not commit any blocks in this epoch (\d+)', log)
        nocounts = { id:self._to_posix(t) for t,id in tmp }
        
        tmp = findall(r'\[INFO] (.*) core.* advance next epoch (\d+)', log)
        epochcounts = { id:self._to_posix(t) for t,id in tmp}
        
        tmp = findall(r'\[INFO] (.*) pool.* Received Batch (\d+)', log)
        batchs = { id:self._to_posix(t) for t,id in tmp}
        
        tmp = findall(r'\[INFO] (.*) core.* create Block epoch \d+ node \d+ batch_id (\d+)', log)
        tmp = { (id,self._to_posix(t)) for t,id in tmp }
        proposals = self._merge_results([tmp])

        tmp = findall(r'\[INFO] (.*) commitor.* commit Block epoch \d+ node \d+ batch_id (\d+)', log)
        tmp = [(id, self._to_posix(t)) for t, id in tmp]
        commits = self._merge_results([tmp])
        
        tmp = findall(r'\[INFO] (.*) commitor.* commit Block epoch (\d+) node \d+ batch_id (\d+)', log)
        commit_epoch_batchid = {id:epoch for _, epoch,id in tmp}


        configs = {
            'consensus': {
                'faults': int(
                    search(r'Consensus DDos: .*, Faults: (\d+)', log).group(1)
                ),
            },
            'pool': {
                'tx_size': int(
                    search(r'Transaction pool tx size set to (\d+)', log).group(1)
                ),
                'batch_size': int(
                    search(r'Transaction pool batch size set to (\d+)', log).group(1)
                ),
                'rate':int(
                    search(r'Transaction pool tx rate set to (\d+)', log).group(1)
                ),
            }
        }

        return commit_epoch_batchid,nocounts,epochcounts,batchs,proposals, commits,configs

    def _to_posix(self, string):
        # 解析时间字符串为 datetime 对象
        dt = datetime.strptime(string, "%Y/%m/%d %H:%M:%S.%f")
        # 转换为 Unix 时间戳
        timestamp = dt.timestamp()
        return timestamp    

    def _consensus_throughput(self):
        if not self.commits:
            return 0, 0
        start, end = min(self.proposals.values()), max(self.commits.values())
        duration = end - start
        tps = len(self.commits)*self.configs['pool']['batch_size'] / duration
        return tps, duration

    def _consensus_latency(self):
        latency = [c - self.proposals[d] for d, c in self.commits.items() if d in self.proposals]
        return mean(latency) if latency else 0

    def _end_to_end_throughput(self):
        if not self.commits:
            return 0, 0
        start, end = min(self.batchs.values()), max(self.commits.values())
        duration = end - start
        tps = len(self.commits)*self.configs['pool']['batch_size'] / duration
        return tps, duration

    def _end_to_end_latency(self):
        latency = []
        for id,t in self.commits.items():
            if id in self.batchs:
                latency += [t-self.batchs[id]]
        return mean(latency) if latency else 0
    
    #所有skip掉的epoch的块的延迟的方差
    def _failed_epoch_commit_latency_variance(self):
        latencies = []
        fail_epochs = {id for id in self.nocounts}

        for batch_id, create_time in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch not in fail_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                latencies+=[(commit_time - create_time)*1000]

        average_latency = mean(latencies) if latencies else 0
        
        
        fail_latencies = []
        failed_epochs = {id for id in self.nocounts}

        for batch_id, create_time in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch in failed_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                fail_latencies.append((commit_time - create_time)*1000)
        
        if fail_latencies:
            squared_diffs = [(x - average_latency) ** 2 for x in fail_latencies]
            variance_against_avg = mean(squared_diffs)
            return variance_against_avg
        else:
            return 0
        
    
    
    
    #所有skip掉的epoch的块的延迟平均值
    def _failed_epoch_commit_latency(self):
        latencies = []
        failed_epochs = {id for id in self.nocounts}

        for batch_id, create_time in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch in failed_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                latencies+=[commit_time - create_time]
        
        return mean(latencies) if latencies else 0
    
    def _failed_epoch_end_to_end_latency(self):
        latencies = []
        failed_epochs = {id for id in self.nocounts}

        for batch_id, _ in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch in failed_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                if batch_id in self.batchs:
                    latencies+=[commit_time - self.batchs[batch_id]]
        return mean(latencies) if latencies else 0
    
    #去掉那些不能在本轮提交的块的平均延迟
    def _normal_epoch_commit_latency(self):
        latencies = []
        failed_epochs = {id for id in self.nocounts}

        for batch_id, create_time in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch not in failed_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                latencies.append(commit_time - create_time)

        return mean(latencies) if latencies else 0
    
    def _normal_epoch_end_to_end_commit_latency(self):
        latencies = []
        failed_epochs = {id for id in self.nocounts}

        for batch_id, _ in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch not in failed_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                if batch_id in self.batchs:
                    latencies.append(commit_time - self.batchs[batch_id])

        return mean(latencies) if latencies else 0
    
    def _failed_epoch_end_to_end_latency_variance(self):
        latencies = []
        fail_epochs = {id for id in self.nocounts}

        for batch_id, _ in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch not in fail_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                if batch_id in self.batchs:
                    latencies.append((commit_time - self.batchs[batch_id])*1000)

        average_latency = mean(latencies) if latencies else 0
        
        fail_latencies = []
        failed_epochs = {id for id in self.nocounts}

        for batch_id, _ in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch in failed_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                if batch_id in self.batchs:
                    fail_latencies.append((commit_time - self.batchs[batch_id])*1000)
        
        if fail_latencies:
            squared_diffs = [(x - average_latency) ** 2 for x in fail_latencies]
            variance_against_avg = mean(squared_diffs)
            return variance_against_avg
        else:
            return 0

    def result(self):
        normal_epoch_commit_latency=self._normal_epoch_commit_latency()*1000
        failed_epoch_commit_latency=self._failed_epoch_commit_latency()*1000
        normal_epoch_end_to_end_latency=self._normal_epoch_end_to_end_commit_latency()*1000
        failed_epoch_end_to_end_latency=self._failed_epoch_end_to_end_latency()*1000
        failed_epoch_commit_latency_variance=self._failed_epoch_commit_latency_variance()
        failed_epoch_end_to_end_latency_variance=self._failed_epoch_end_to_end_latency_variance()
        
        consensus_latency = self._consensus_latency() * 1000
        consensus_tps, _ = self._consensus_throughput()
        end_to_end_tps, duration = self._end_to_end_throughput()
        end_to_end_latency = self._end_to_end_latency() * 1000
        nocounts = len(self.nocounts)
        commitcount=len(self.commits)
        epochcounts=len(self.epochcounts)
        tx_size = self.configs['pool']['tx_size']
        batch_size = self.configs['pool']['batch_size']
        rate = self.configs['pool']['rate']
        return (
            '\n'
            '-----------------------------------------\n'
            ' SUMMARY:\n'
            '-----------------------------------------\n'
            ' + CONFIG:\n'
            f' Protocol: {self.protocol} \n'
            f' DDOS attack: {self.ddos} \n'
            f' Committee size: {self.committee_size} nodes\n'
            f' Input rate: {rate:,} tx/s\n'
            f' Transaction size: {tx_size:,} B\n'
            f' Batch size: {batch_size:,} tx/Batch\n'
            f' Faults: {self.faults} nodes\n'
            f' Execution time: {round(duration):,} s\n'
            '\n'
            ' + RESULTS:\n'
            f' Consensus TPS: {round(consensus_tps):,} tx/s\n'
            f' Consensus latency: {round(consensus_latency):,} ms\n'
            '\n'
            f' End-to-end TPS: {round(end_to_end_tps):,} tx/s\n'
            f' End-to-end latency: {round(end_to_end_latency):,} ms\n'
            f' The epoch count can not commit block: {round(nocounts):,}\n'
            f' The all epoch counts : {round(epochcounts):,}\n'
            f' The all epoch count commit block: {round(commitcount):,}\n'
            f' failed_epoch_commit_latency: {round(failed_epoch_commit_latency):,}ms\n'
            f' normal_epoch_commit_latency: {round(normal_epoch_commit_latency):,}ms\n'
            f' failed_epoch_commit_latency_variance: {round(failed_epoch_commit_latency_variance):,}ms\n'
            f' failed_epoch_end_to_end_latency: {round(failed_epoch_end_to_end_latency):,}ms\n'
            f' normal_epoch_end_to_end_latency: {round(normal_epoch_end_to_end_latency):,}ms\n'
            f' failed_epoch_end_to_end_latency_variance: {round(failed_epoch_end_to_end_latency_variance):,}ms\n'
            
            '-----------------------------------------\n'
        )
    def fail_commit_latency(self):
        latencies = []
        fail_epochs = {id for id in self.nocounts}

        for batch_id, create_time in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch not in fail_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                latencies+=[(commit_time - create_time)]

        average_latency = mean(latencies) if latencies else 0
        flatencies = []
        ffailed_epochs = {id for id in self.nocounts}

        for batch_id, create_time in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch in ffailed_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                latency = commit_time - create_time -average_latency
                flatencies.append((int(epoch), latency * 1000))  # 转成毫秒
        flatencies.sort(key=lambda x: x[0])
        return "\n".join(f"{epoch} {latency:.3f}" for epoch, latency in flatencies)
    
    def normal_commit_latency(self):
        latencies = []
        fail_epochs = {id for id in self.nocounts}

        for batch_id, create_time in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch not in fail_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                latency=(commit_time - create_time)
                latencies.append((int(epoch), latency * 1000))

        average_latency = mean([lat for _, lat in latencies]) if latencies else 0
        latencies = [(epoch, latency - average_latency) for epoch, latency in latencies]
        latencies.sort(key=lambda x: x[0])
        return "\n".join(f"{epoch} {latency:.3f}" for epoch, latency in latencies)
    
    def normal_end_to_end_latency(self):
        latencies = []
        fail_epochs = {id for id in self.nocounts}

        for batch_id, _ in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch not in fail_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                if batch_id in self.batchs:
                    latency=(commit_time - self.batchs[batch_id])
                    latencies.append((int(epoch), latency * 1000))

        average_latency = mean([lat for _, lat in latencies]) if latencies else 0
        latencies = [(epoch, latency - average_latency) for epoch, latency in latencies]
        latencies.sort(key=lambda x: x[0])
        return "\n".join(f"{epoch} {latency:.3f}" for epoch, latency in latencies)
    
    def fail_end_to_end_latency(self):
        latencies = []
        fail_epochs = {id for id in self.nocounts}

        for batch_id, _ in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch not in fail_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                if batch_id in self.batchs:
                    latencies.append((commit_time - self.batchs[batch_id]))

        average_latency = mean(latencies) if latencies else 0
        
        
        flatencies = []
        ffailed_epochs = {id for id in self.nocounts}

        for batch_id, _ in self.proposals.items():
            epoch = self.commit_epoch_batchid.get(batch_id)
            if epoch in ffailed_epochs and batch_id in self.commits:
                commit_time = self.commits[batch_id]
                if batch_id in self.batchs:
                    latency = commit_time - self.batchs[batch_id]-average_latency
                    flatencies.append((int(epoch), latency * 1000))  # 转成毫秒
        flatencies.sort(key=lambda x: x[0])
        return "\n".join(f"{epoch} {latency:.3f}" for epoch, latency in flatencies)
        
        
    def print(self, filename):
        assert isinstance(filename, str)
        with open(filename, 'a') as f:
            f.write(self.result())
    
    def printfail_commit_latency(self,filename1,filename2):
        assert isinstance(filename1, str)
        with open(filename1, 'a') as f:
            f.write(self.fail_commit_latency())
        assert isinstance(filename2, str)
        with open(filename2, 'a') as f:
            f.write(self.normal_commit_latency())
            
    def printfail_end_to_end_latency(self,filename1,filename2):
        assert isinstance(filename1, str)
        with open(filename1, 'a') as f:
            f.write(self.fail_end_to_end_latency())
        assert isinstance(filename2, str)
        with open(filename2, 'a') as f:
            f.write(self.normal_end_to_end_latency())

    @classmethod
    def process(cls, directory, faults=0, protocol="", ddos=False):
        assert isinstance(directory, str)

        nodes = []
        for filename in sorted(glob(join(directory, 'node-info-*.log'))):
            with open(filename, 'r') as f:
                nodes += [f.read()]

        return cls(nodes, faults=faults, protocol=protocol, ddos=ddos)